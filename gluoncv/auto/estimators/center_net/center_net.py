"""CenterNet Estimator"""
# pylint: disable=unused-variable,missing-function-docstring
import os
import time
import warnings
from collections import OrderedDict

import pandas as pd
import numpy as np
import mxnet as mx
from mxnet import autograd
from mxnet import gluon

from ..base_estimator import BaseEstimator, set_default
from ....data.batchify import Tuple, Stack, Pad
from ....data.transforms.presets.center_net import CenterNetDefaultTrainTransform
from ....data.transforms.presets.center_net import CenterNetDefaultValTransform
from ....data.transforms.presets.center_net import load_test, transform_test
from ....loss import MaskedL1Loss, HeatmapFocalLoss
from ....model_zoo import get_model
from ....model_zoo.center_net import get_center_net, get_base_network
from ....utils import LRScheduler, LRSequential
from ....utils.metrics import VOCMApMetric, VOC07MApMetric
from .default import CenterNetCfg

__all__ = ['CenterNetEstimator']

@set_default(CenterNetCfg())
class CenterNetEstimator(BaseEstimator):
    """Estimator implementation for CenterNet.

    Parameters
    ----------
    config : dict
        Config in nested dict.
    logger : logging.Logger
        Optional logger for this estimator, can be `None` when default setting is used.
    reporter : callable
        The reporter for metric checkpointing.

    """
    def __init__(self, config, logger=None, reporter=None):
        super(CenterNetEstimator, self).__init__(config, logger, reporter=reporter, name=None)
        self.last_train = None

    def _predict(self, x):
        short_size = min(self._cfg.center_net.data_shape)
        if isinstance(x, str):
            x = load_test(x, short=short_size, max_size=1024)[0]
        elif isinstance(x, mx.nd.NDArray):
            x = transform_test(x, short=short_size, max_size=1024)[0]
        elif isinstance(x, pd.DataFrame):
            assert 'image' in x.columns, "Expect column `image` for input images"
            def _predict_merge(x):
                y = self._predict(x)
                y['image'] = x
                return y
            return pd.concat([_predict_merge(xx) for xx in x['image']]).reset_index(drop=True)
        else:
            raise ValueError('Input is not supported: {}'.format(type(x)))
        height, width = x.shape[2:4]
        x = x.as_in_context(self.ctx[0])
        ids, scores, bboxes = [xx[0].asnumpy() for xx in self.net(x)]
        bboxes[:, (0, 2)] /= width
        bboxes[:, (1, 3)] /= height
        bboxes = np.clip(bboxes, 0.0, 1.0).tolist()
        df = pd.DataFrame({'predict_class': [self.classes[int(id)] for id in ids], 'predict_score': scores,
                           'predict_rois': [{'xmin': bbox[0], 'ymin': bbox[1], 'xmax': bbox[2], 'ymax': bbox[3]} \
                                for bbox in bboxes]})
        # filter out invalid (scores < 0) rows
        valid_df = df[df['predict_score'] > 0].reset_index(drop=True)
        return valid_df

    def _fit(self, train_data, val_data):
        self._best_map = 0
        self.epoch = 0
        self._time_elapsed = 0
        if max(self._cfg.train.start_epoch, self.epoch) >= self._cfg.train.epochs:
            return {'time', self._time_elapsed}
        if not isinstance(train_data, pd.DataFrame):
            self.last_train = len(train_data)
        else:
            self.last_train = train_data
        self._init_trainer()
        return self._resume_fit(train_data, val_data)

    def _resume_fit(self, train_data, val_data):
        if max(self._cfg.train.start_epoch, self.epoch) >= self._cfg.train.epochs:
            return {'time', self._time_elapsed}
        if not self.classes or not self.num_class:
            raise ValueError('Unable to determine classes of dataset')
        train_dataset = train_data.to_mxnet()
        val_dataset = val_data.to_mxnet()

        # dataloader
        batch_size = self._cfg.train.batch_size
        width, height = self._cfg.center_net.data_shape
        num_class = len(train_dataset.classes)
        batchify_fn = Tuple([Stack() for _ in range(6)])  # stack image, cls_targets, box_targets
        train_loader = gluon.data.DataLoader(
            train_dataset.transform(CenterNetDefaultTrainTransform(
                width, height, num_class=num_class, scale_factor=self.net.scale)),
            batch_size, True, batchify_fn=batchify_fn, last_batch='rollover',
            num_workers=self._cfg.train.num_workers)
        val_batchify_fn = Tuple(Stack(), Pad(pad_val=-1))
        val_loader = gluon.data.DataLoader(
            val_dataset.transform(CenterNetDefaultValTransform(width, height)),
            self._cfg.valid.batch_size, False, batchify_fn=val_batchify_fn, last_batch='keep',
            num_workers=self._cfg.valid.num_workers)
        train_eval_loader = gluon.data.DataLoader(
            train_dataset.transform(CenterNetDefaultValTransform(width, height)),
            self._cfg.valid.batch_size, False, batchify_fn=val_batchify_fn, last_batch='keep',
            num_workers=self._cfg.valid.num_workers)

        return self._train_loop(train_loader, val_loader, train_eval_loader)

    def _train_loop(self, train_data, val_data, train_eval_data):
        wh_loss = MaskedL1Loss(weight=self._cfg.center_net.wh_weight)
        heatmap_loss = HeatmapFocalLoss(from_logits=True)
        center_reg_loss = MaskedL1Loss(weight=self._cfg.center_net.center_reg_weight)
        heatmap_loss_metric = mx.metric.Loss('HeatmapFocal')
        wh_metric = mx.metric.Loss('WHL1')
        center_reg_metric = mx.metric.Loss('CenterRegL1')

        self._logger.info('Start training from [Epoch %d]', max(self._cfg.train.start_epoch, self.epoch))
        for self.epoch in range(max(self._cfg.train.start_epoch, self.epoch), self._cfg.train.epochs):
            wh_metric.reset()
            center_reg_metric.reset()
            heatmap_loss_metric.reset()
            tic = time.time()
            btic = time.time()
            self.net.hybridize()
            epoch = self.epoch

            for i, batch in enumerate(train_data):
                split_data = [
                    gluon.utils.split_and_load(batch[ind], ctx_list=self.ctx, batch_axis=0) for ind
                    in range(6)]
                data, heatmap_targets, wh_targets, wh_masks, center_reg_targets, center_reg_masks = split_data
                batch_size = self._cfg.train.batch_size
                with autograd.record():
                    sum_losses = []
                    heatmap_losses = []
                    wh_losses = []
                    center_reg_losses = []
                    wh_preds = []
                    center_reg_preds = []
                    for x, heatmap_target, wh_target, wh_mask, center_reg_target, center_reg_mask in zip(
                            *split_data):
                        heatmap_pred, wh_pred, center_reg_pred = self.net(x)
                        wh_preds.append(wh_pred)
                        center_reg_preds.append(center_reg_pred)
                        wh_losses.append(wh_loss(wh_pred, wh_target, wh_mask))
                        center_reg_losses.append(
                            center_reg_loss(center_reg_pred, center_reg_target, center_reg_mask))
                        heatmap_losses.append(heatmap_loss(heatmap_pred, heatmap_target))
                        curr_loss = heatmap_losses[-1] + wh_losses[-1] + center_reg_losses[-1]
                        sum_losses.append(curr_loss)
                    autograd.backward(sum_losses)
                self.trainer.step(len(sum_losses))  # step with # gpus

                heatmap_loss_metric.update(0, heatmap_losses)
                wh_metric.update(0, wh_losses)
                center_reg_metric.update(0, center_reg_losses)
                if self._cfg.train.log_interval and not (i + 1) % self._cfg.train.log_interval:
                    name2, loss2 = wh_metric.get()
                    name3, loss3 = center_reg_metric.get()
                    name4, loss4 = heatmap_loss_metric.get()
                    self._logger.info(
                        '[Epoch {}][Batch {}], Speed: {:.3f} samples/sec, '
                        'LR={}, {}={:.3f}, {}={:.3f}, {}={:.3f}'.format(
                            epoch, i, batch_size / (time.time() - btic),
                            self.trainer.learning_rate, name2, loss2, name3, loss3, name4, loss4))
                btic = time.time()

            name2, loss2 = wh_metric.get()
            name3, loss3 = center_reg_metric.get()
            name4, loss4 = heatmap_loss_metric.get()
            self._logger.info(
                '[Epoch {}] Training cost: {:.3f}, {}={:.3f}, {}={:.3f}, {}={:.3f}'.format(
                    epoch, (time.time() - tic), name2, loss2, name3, loss3, name4, loss4))
            if (epoch % self._cfg.valid.interval == 0) or (epoch == self._cfg.train.epochs - 1):
                # consider reduce the frequency of validation to save time
                map_name, mean_ap = self._evaluate(val_data)
                val_msg = '\n'.join(['{}={}'.format(k, v) for k, v in zip(map_name, mean_ap)])
                self._logger.info('[Epoch %d] Validation: \n%s', epoch, val_msg)
                current_map = float(mean_ap[-1])
                if current_map > self._best_map:
                    cp_name = os.path.join(self._logdir, 'best_checkpoint.pkl')
                    self._logger.info('[Epoch %d] Current best map: %f vs previous %f, saved to %s',
                                      self.epoch, current_map, self._best_map, cp_name)
                    self.save(cp_name)
                    self._best_map = current_map
                if self._reporter:
                    self._reporter(epoch=epoch, map_reward=current_map)
            self._time_elapsed += time.time() - btic
        # map on train data
        map_name, mean_ap = self._evaluate(train_eval_data)
        return {'train_map': float(mean_ap[-1]), 'valid_map': self._best_map, 'time': self._time_elapsed}

    def _evaluate(self, val_data):
        """Test on validation dataset."""
        if self._cfg.valid.metric == 'voc07':
            eval_metric = VOC07MApMetric(iou_thresh=self._cfg.valid.iou_thresh, class_names=self.classes)
        elif self._cfg.valid.metric == 'voc':
            eval_metric = VOCMApMetric(iou_thresh=self._cfg.valid.iou_thresh, class_names=self.classes)
        else:
            raise ValueError(f'Invalid metric type: {self._cfg.valid.metric}')
        self.net.flip_test = self._cfg.valid.flip_test
        mx.nd.waitall()
        self.net.hybridize()
        if not isinstance(val_data, gluon.data.DataLoader):
            from ...tasks.dataset import ObjectDetectionDataset
            if isinstance(val_data, ObjectDetectionDataset):
                val_data = val_data.to_mxnet()
            val_batchify_fn = Tuple(Stack(), Pad(pad_val=-1))
            width, height = self._cfg.center_net.data_shape
            val_data = gluon.data.DataLoader(
                val_data.transform(CenterNetDefaultValTransform(width, height)),
                self._cfg.valid.batch_size, False, batchify_fn=val_batchify_fn, last_batch='keep',
                num_workers=self._cfg.valid.num_workers)
        for batch in val_data:
            data = gluon.utils.split_and_load(batch[0], ctx_list=self.ctx, batch_axis=0,
                                              even_split=False)
            label = gluon.utils.split_and_load(batch[1], ctx_list=self.ctx, batch_axis=0,
                                               even_split=False)
            det_bboxes = []
            det_ids = []
            det_scores = []
            gt_bboxes = []
            gt_ids = []
            gt_difficults = []
            for x, y in zip(data, label):
                # get prediction results
                ids, scores, bboxes = self.net(x)
                det_ids.append(ids)
                det_scores.append(scores)
                # clip to image size
                det_bboxes.append(bboxes.clip(0, batch[0].shape[2]))
                # split ground truths
                gt_ids.append(y.slice_axis(axis=-1, begin=4, end=5))
                gt_bboxes.append(y.slice_axis(axis=-1, begin=0, end=4))
                gt_difficults.append(
                    y.slice_axis(axis=-1, begin=5, end=6) if y.shape[-1] > 5 else None)

            # update metric
            eval_metric.update(det_bboxes, det_ids, det_scores, gt_bboxes, gt_ids, gt_difficults)
        return eval_metric.get()

    def _init_network(self):
        if not self.num_class:
            raise ValueError('Unable to create network when `num_class` is unknown. \
                It should be inferred from dataset or resumed from saved states.')
        assert len(self.classes) == self.num_class
        # network
        ctx = [mx.gpu(int(i)) for i in self._cfg.train.gpus]
        ctx = ctx if ctx else [mx.cpu()]
        self.ctx = ctx
        if self._cfg.center_net.transfer is not None:
            assert isinstance(self._cfg.center_net.transfer, str)
            self._logger.info('Using transfer learning from %s, ignoring some of the network configs',
                              self._cfg.center_net.transfer)
            net = get_model(self._cfg.center_net.transfer, pretrained=True)
            net.reset_class(self.classes, reuse_weights=[cname for cname in self.classes if cname in net.classes])
        else:
            net_name = '_'.join(('center_net', self._cfg.center_net.base_network, self.dataset))
            heads = OrderedDict([
                ('heatmap',
                 {'num_output': self.num_class, 'bias': self._cfg.center_net.heads.bias}),
                ('wh', {'num_output': self._cfg.center_net.heads.wh_outputs}),
                ('reg', {'num_output': self._cfg.center_net.heads.reg_outputs})])
            base_network = get_base_network(self._cfg.center_net.base_network,
                                            pretrained=self._cfg.train.pretrained_base)
            net = get_center_net(self._cfg.center_net.base_network,
                                 self.dataset,
                                 base_network=base_network,
                                 heads=heads,
                                 head_conv_channel=self._cfg.center_net.heads.head_conv_channel,
                                 classes=self.classes,
                                 scale=self._cfg.center_net.scale,
                                 topk=self._cfg.center_net.topk,
                                 norm_layer=gluon.nn.BatchNorm)
            with warnings.catch_warnings(record=True) as w:
                warnings.simplefilter("always")
                net.initialize()
        self.net = net
        for k, v in self.net.collect_params('.*bias').items():
            v.wd_mult = 0.0
        self.net.collect_params().reset_ctx(self.ctx)

    def _init_trainer(self):
        if self.last_train is None:
            raise RuntimeError('Cannot init trainer without knowing the size of training data')
        if isinstance(self.last_train, pd.DataFrame):
            train_size = len(self.last_train)
        elif isinstance(self.last_train, int):
            train_size = self.last_train
        else:
            raise ValueError("Unknown type of self.last_train: {}".format(type(self.last_train)))
        lr_decay = float(self._cfg.train.lr_decay)
        lr_steps = sorted(self._cfg.train.lr_decay_epoch)
        lr_decay_epoch = [e - self._cfg.train.warmup_epochs for e in lr_steps]
        num_batches = train_size // self._cfg.train.batch_size
        lr_scheduler = LRSequential([
            LRScheduler('linear', base_lr=0, target_lr=self._cfg.train.lr,
                        nepochs=self._cfg.train.warmup_epochs, iters_per_epoch=num_batches),
            LRScheduler(self._cfg.train.lr_mode, base_lr=self._cfg.train.lr,
                        nepochs=self._cfg.train.epochs - self._cfg.train.warmup_epochs,
                        iters_per_epoch=num_batches,
                        step_epoch=lr_decay_epoch,
                        step_factor=self._cfg.train.lr_decay, power=2),
        ])
        self.trainer = gluon.Trainer(
            self.net.collect_params(), 'adam',
            {'learning_rate': self._cfg.train.lr, 'wd': self._cfg.train.wd,
             'lr_scheduler': lr_scheduler})