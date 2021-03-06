# Copyright 2020 Jiang Shenghu
# SPDX-License-Identifier: Apache-2.0
from .utils import *
from ..op import CombinedOp, SequenceOp, ElementwiseAdd, Linear, ReLU6


def _make_divisible(v, divisor, min_value=None):
    if min_value is None:
        min_value = divisor
    new_v = max(min_value, int(v + divisor / 2) // divisor * divisor)
    if new_v < 0.9 * v:
        new_v += divisor
    return new_v


class Conv2dReLU6(CombinedOp):
    def __init__(self, name, in_shape, out_channel, kernel_size=3, stride=1, groups=1):
        in_shape = shape2d(in_shape)
        padding = (kernel_size - 1) // 2
        if groups != 1:
            self.conv = grouped_conv(name + '.conv', in_shape, out_channel, kernel_size,
                                     pad=padding, stride=stride, groups=groups, biased=False)
        else:
            self.conv = conv(name + '.conv', in_shape, out_channel, kernel_size,
                             pad=padding, stride=stride, biased=False)
        self.relu = mock(ReLU6, name + '.relu', self.conv)
        self.batch = self.conv.batch
        self.out_channel = self.conv.out_channel
        self.out_height = self.conv.out_height
        self.out_width = self.conv.out_width
        ops = [self.conv, self.relu]
        super().__init__(name=name, ops=ops)

    def calc(self, x):
        out = self.conv.calc(x)
        out = self.relu.calc(out)
        return out


class InvertedResidual(CombinedOp):
    def __init__(self, name, in_shape, out_channel, stride, expand_ratio):
        self.stride = stride
        self.expand_ratio = expand_ratio
        assert stride in [1, 2]

        in_shape = shape2d(in_shape)
        input_channel = in_shape[1]
        hidden_dim = int(round(input_channel * expand_ratio))
        self.use_res_connect = self.stride == 1 and input_channel == out_channel

        if expand_ratio != 1:
            self.conv0 = Conv2dReLU6(name + '.conv0', in_shape, hidden_dim, kernel_size=1)
            self.conv1 = Conv2dReLU6(name + '.conv1', self.conv0, hidden_dim,
                                     stride=stride, groups=hidden_dim)
        else:
            self.conv1 = Conv2dReLU6(name + '.conv1', in_shape, hidden_dim,
                                     stride=stride, groups=hidden_dim)
        self.conv2 = conv(name + '.conv2', self.conv1, out_channel, 1, stride=1, pad=0, biased=False)
        self.batch = self.conv2.batch
        self.out_channel = self.conv2.out_channel
        self.out_height = self.conv2.out_height
        self.out_width = self.conv2.out_width
        if self.use_res_connect:
            self.eltwise_add = mock(ElementwiseAdd, name + '.eltwise_add', self.conv2)
        ops = [v for v in self.__dict__.values() if isinstance(v, BaseOp)]
        super().__init__(name=name, ops=ops)

    def calc(self, x):
        if self.expand_ratio != 1:
            out = self.conv0.calc(x)
            out = self.conv1.calc(out)
        else:
            out = self.conv1.calc(x)
        out = self.conv2.calc(out)
        if self.use_res_connect:
            return self.eltwise_add.calc(x, out)
        else:
            return out


class MobileNetV2(CombinedOp):
    def __init__(self, name, input_shape,
                 num_classes=1000,
                 width_mult=1.0,
                 inverted_residual_setting=None,
                 round_nearest=8,
                 block=None):
        self.name = name

        if block is None:
            block = InvertedResidual
        input_channel = 32
        last_channel = 1280

        if inverted_residual_setting is None:
            inverted_residual_setting = [
                # t, c, n, s
                [1, 16, 1, 1],
                [6, 24, 2, 2],
                [6, 32, 3, 2],
                [6, 64, 4, 2],
                [6, 96, 3, 1],
                [6, 160, 3, 2],
                [6, 320, 1, 1],
            ]

        # only check the first element, assuming user knows t,c,n,s are required
        if len(inverted_residual_setting) == 0 or len(inverted_residual_setting[0]) != 4:
            raise ValueError("inverted_residual_setting should be non-empty "
                             "or a 4-element list, got {}".format(inverted_residual_setting))

        # building first layer
        input_channel = _make_divisible(input_channel * width_mult, round_nearest)
        self.last_channel = _make_divisible(last_channel * max(1.0, width_mult), round_nearest)
        features = [Conv2dReLU6(name + '.features[0]', input_shape, input_channel, stride=2)]
        # building inverted residual blocks
        for t, c, n, s in inverted_residual_setting:
            output_channel = _make_divisible(c * width_mult, round_nearest)
            for i in range(n):
                stride = s if i == 0 else 1
                features.append(block(name + f'.features[{len(features)}]',
                                      features[-1], output_channel, stride, expand_ratio=t))
        # building last several layers
        features.append(Conv2dReLU6(name + f'.features[{len(features)}]',
                                    features[-1], self.last_channel, kernel_size=1))
        # make it nn.Sequential
        self.features = SequenceOp(name=name + '.features', ops=features)

        # building classifier
        self.pool = adaptive_pool(name + '.pool', self.features, 1, 1, 'avg')
        self.flatten = flatten2d(name + '.flatten', self.pool)
        self.fc = Linear(
            batch=self.flatten.batch, in_channel=self.last_channel,
            out_channel=num_classes, biased=True,
            name=name + '.linear'
        )

        ops = [v for v in self.__dict__.values() if isinstance(v, BaseOp)]

        super().__init__(name=name, ops=ops)

    def _forward_impl(self, x):
        # This exists since TorchScript doesn't support inheritance, so the superclass method
        # (this one) needs to have a name other than `forward` that can be accessed in a subclass
        x = self.features.calc(x)
        x = self.pool.calc(x)
        x = self.flatten.calc(x)
        x = self.fc.calc(x)
        return x

    def calc(self, x):
        return self._forward_impl(x)
