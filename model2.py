import math

import torch
from torch import nn
from torch.nn import functional as F

from stylegan2.model import StyledConv, Blur, EqualLinear, EqualConv2d, ScaledLeakyReLU
from stylegan2.op import FusedLeakyReLU

class SELayer(nn.Module):
    def __init__(self, channel, reduction=16):
        super(SELayer, self).__init__()
        self.avg_pool = nn.AdaptiveAvgPool2d(1)
        self.fc = nn.Sequential(
            nn.Linear(channel, channel // reduction, bias=False),
            nn.ReLU(inplace=True),
            nn.Linear(channel // reduction, 2 * channel, bias=False),
            nn.Sigmoid()
        )

    def forward(self, x):
        b, c, _, _ = x.size()
        y = self.avg_pool(x).view(b, c)
        y = self.fc(y).view(b, 2 * c, 1, 1)
        return x * y[:,:c,:,:].expand_as(x),x * y[:,c:,:,:].expand_as(x)

class EqualConvTranspose2d(nn.Module):
    def __init__(
        self, in_channel, out_channel, kernel_size, stride=1, padding=0, bias=True
    ):
        super().__init__()

        self.weight = nn.Parameter(
            torch.randn(in_channel, out_channel, kernel_size, kernel_size)
        )
        self.scale = 1 / math.sqrt(in_channel * kernel_size ** 2)

        self.stride = stride
        self.padding = padding

        if bias:
            self.bias = nn.Parameter(torch.zeros(out_channel))

        else:
            self.bias = None

    def forward(self, input):
        out = F.conv_transpose2d(
            input,
            self.weight * self.scale,
            bias=self.bias,
            stride=self.stride,
            padding=self.padding,
        )

        return out

    def __repr__(self):
        return (
            f"{self.__class__.__name__}({self.weight.shape[0]}, {self.weight.shape[1]},"
            f" {self.weight.shape[2]}, stride={self.stride}, padding={self.padding})"
        )


class ConvLayer(nn.Sequential):
    def __init__(
        self,
        in_channel,
        out_channel,
        kernel_size,
        upsample=False,
        downsample=False,
        blur_kernel=(1, 3, 3, 1),
        bias=True,
        activate=True,
        padding="zero",
    ):
        layers = []

        self.padding = 0
        stride = 1

        if downsample:
            factor = 2
            p = (len(blur_kernel) - factor) + (kernel_size - 1)
            pad0 = (p + 1) // 2
            pad1 = p // 2

            layers.append(Blur(blur_kernel, pad=(pad0, pad1)))

            stride = 2

        if upsample:
            layers.append(
                EqualConvTranspose2d(
                    in_channel,
                    out_channel,
                    kernel_size,
                    padding=0,
                    stride=2,
                    bias=bias and not activate,
                )
            )

            factor = 2
            p = (len(blur_kernel) - factor) - (kernel_size - 1)
            pad0 = (p + 1) // 2 + factor - 1
            pad1 = p // 2 + 1

            layers.append(Blur(blur_kernel, pad=(pad0, pad1)))

        else:
            if not downsample:
                if padding == "zero":
                    self.padding = (kernel_size - 1) // 2

                elif padding == "reflect":
                    padding = (kernel_size - 1) // 2

                    if padding > 0:
                        layers.append(nn.ReflectionPad2d(padding))

                    self.padding = 0

                elif padding != "valid":
                    raise ValueError('Padding should be "zero", "reflect", or "valid"')

            layers.append(
                EqualConv2d(
                    in_channel,
                    out_channel,
                    kernel_size,
                    padding=self.padding,
                    stride=stride,
                    bias=bias and not activate,
                )
            )

        if activate:
            if bias:
                layers.append(FusedLeakyReLU(out_channel))

            else:
                layers.append(ScaledLeakyReLU(0.2))

        super().__init__(*layers)


class StyledResBlock(nn.Module):
    def __init__(
        self, in_channel, out_channel, style_dim, upsample, blur_kernel=(1, 3, 3, 1)
    ):
        super().__init__()

        self.conv1 = StyledConv(
            in_channel,
            out_channel,
            3,
            style_dim,
            upsample=upsample,
            blur_kernel=blur_kernel,
        )
        self.upsample=upsample

        self.conv2 = StyledConv(out_channel, out_channel, 3, style_dim)

        if upsample or in_channel != out_channel:
            self.skip = ConvLayer(
                in_channel,
                out_channel,
                1,
                upsample=False, 
                blur_kernel=blur_kernel,
                bias=False,
                activate=False,
            )

        else:
            self.skip = None

    def forward(self, input, style, noise=None):
        out = self.conv1(input, style, noise)
        out = self.conv2(out, style, noise)

        if self.skip is not None:
            skip = self.skip(input)
            if self.upsample:
                skip = F.interpolate(skip, scale_factor=2, mode='bilinear', align_corners=False)

        else:
            skip = input

        return (out + skip) / math.sqrt(2)


class ResBlock(nn.Module):
    def __init__(
        self,
        in_channel,
        out_channel,
        downsample,
        padding="zero",
        blur_kernel=(1, 3, 3, 1),
    ):
        super().__init__()

        self.conv1 = ConvLayer(in_channel, out_channel, 3, padding=padding)

        self.conv2 = ConvLayer(
            out_channel,
            out_channel,
            3,
            downsample=downsample,
            padding=padding,
            blur_kernel=blur_kernel,
        )

        if downsample or in_channel != out_channel:
            self.skip = ConvLayer(
                in_channel,
                out_channel,
                1,
                downsample=downsample,
                blur_kernel=blur_kernel,
                bias=False,
                activate=False,
            )

        else:
            self.skip = None

    def forward(self, input):
        out = self.conv1(input)
        out = self.conv2(out)

        if self.skip is not None:
            skip = self.skip(input)

        else:
            skip = input


        return (out + skip) / math.sqrt(2)

'''
class Encoder(nn.Module):
    def __init__(
        self,
        channel,
        structure_channel=8,
        texture_channel=2048,
        blur_kernel=(1, 3, 3, 1),
    ):
        super().__init__()

        stem = [ConvLayer(3, channel, 1)]

        in_channel = channel
        for i in range(1, 4):
            ch = channel * (2 ** i)
            stem.append(ResBlock(in_channel, ch, downsample=True, padding="reflect"))
            in_channel = ch

        self.stem = nn.Sequential(*stem)

        self.se_layer= nn.Sequential(
            SELayer(in_channel)
        )

        out_channel=channel * (2 ** 4)
        self.structure = nn.Sequential(
            ResBlock(in_channel, out_channel, downsample=True, padding="reflect"),
            ConvLayer(out_channel, out_channel, 1), ConvLayer(out_channel, structure_channel, 1)
        )

        self.texture = nn.Sequential(
            ResBlock(in_channel, out_channel, downsample=True, padding="reflect"),
            ConvLayer(out_channel, out_channel * 2, 3, downsample=True, padding="valid"),
            ConvLayer(out_channel * 2, out_channel * 4, 3, downsample=True, padding="valid"),
            nn.AdaptiveAvgPool2d(1),
            nn.Flatten(1),
            EqualLinear(out_channel * 4, out_channel * 4)
        )

    def forward(self, input):
        out = self.stem(input)
        out_structure, out_texture = self.se_layer(out)
        structure = self.structure(out_structure)
        texture = torch.flatten(self.texture(out_texture), 1)

        return structure, texture
'''


'''
class Generator(nn.Module):
    def __init__(
        self,
        channel,
        structure_channel=8,
        texture_channel=2048,
        blur_kernel=(1, 3, 3, 1),
    ):
        super().__init__()

        ch_multiplier = (4, 8, 16, 16, 8, 4)
        upsample = (False, False, True, True, True, True)

        self.layers = nn.ModuleList()
        in_ch = structure_channel
        for ch_mul, up in zip(ch_multiplier, upsample):
            self.layers.append(
                StyledResBlock(
                    in_ch, channel * ch_mul, texture_channel, up, blur_kernel
                )
            )
            in_ch = channel * ch_mul

        self.to_rgb = ConvLayer(in_ch, 3, 1, activate=False)

    def forward(self, structure, texture, noises=None):
        if noises is None:
            noises = [None] * len(self.layers)

        out = structure
        for layer, noise in zip(self.layers, noises):
            out = layer(out, texture, noise)

        out = self.to_rgb(out)

        return out
'''
class Generator(nn.Module):
    def __init__(
        self,
        channel,
        structure_channel=8,
        texture_channel=2048,
        blur_kernel=(1, 3, 3, 1),
    ):
        super().__init__()

        self.e1 = ConvLayer(3, channel, 1) #32
        self.d1 = ResBlock(channel, channel * (2 ** 1), downsample=True, padding="reflect") #64
        self.d2 = ResBlock(channel * (2 ** 1), channel * (2 ** 2), downsample=True, padding="reflect") #128        
        self.d3 = ResBlock(channel * (2 ** 2), channel * (2 ** 3), downsample=True, padding="reflect") #256
        self.se1 = SELayer(channel * (2 ** 3))

        self.struct1 = ResBlock(channel * (2 ** 3), channel * (2 ** 4), downsample=True, padding="reflect") #512
        self.struct2 = ConvLayer(channel * (2 ** 4), channel * (2 ** 4), 1) #512
        self.struct3 = ConvLayer(channel * (2 ** 4), structure_channel, 1) #8

        self.text1 = ResBlock(channel * (2 ** 3), channel * (2 ** 4), downsample=True, padding="reflect") #512
        self.text2 = ConvLayer(channel * (2 ** 4), channel * (2 ** 4) * 2, 3, downsample=True, padding="valid") #1024
        self.text3 = ConvLayer(channel * (2 ** 4) * 2, channel * (2 ** 4) * 4, 3, downsample=True, padding="valid") #2048
        self.text4 = nn.AdaptiveAvgPool2d(1)
        self.text5 = nn.Flatten(1)
        self.text6 = EqualLinear(channel * (2 ** 4) * 4, channel * (2 ** 4) * 4) #2048


        self.g1 = StyledResBlock(structure_channel, channel * 4, texture_channel, False, blur_kernel) #128
        self.g2 = StyledResBlock(channel * 4, channel * 8, texture_channel, False, blur_kernel) #256
        self.g3 = StyledResBlock(channel * 8 * 3, channel * 16, texture_channel, True, blur_kernel) #512
        self.g4 = StyledResBlock(channel * 8 * 3, channel * 16, texture_channel, True, blur_kernel) #512
        self.g5 = StyledResBlock(channel * 4 * 5, channel * 8, texture_channel, True, blur_kernel) #256 
        self.g6 = StyledResBlock(channel * 10, channel * 4, texture_channel, True, blur_kernel) #128

        self.to_rgb = ConvLayer(channel * 5, 3, 1, activate=False)

    def forward(self, input1, input2, s=0):
        #Real_img1
        in1 = self.e1(input1)
        in2 = self.d1(in1)
        in3 = self.d2(in2)
        in4 = self.d3(in3)
        in5, in6 = self.se1(in4)
        
        s1 = self.struct1(in5)
        s2 = self.struct2(s1)
        s3 = self.struct3(s2)
        
        t1 = self.text1(in6)
        t2 = self.text2(t1)
        t3 = self.text3(t2)
        t4 = self.text4(t3)
        t5 = self.text5(t4)
        t6 = self.text6(t5)
        #Real_img2
        in11 = self.e1(input2)
        in22 = self.d1(in11)
        in33 = self.d2(in22)
        in44 = self.d3(in33)
        in55, in66 = self.se1(in44)
        
        s11 = self.struct1(in55)
        s22 = self.struct2(s11)
        s33 = self.struct3(s22)
        
        t11 = self.text1(in66)
        t22 = self.text2(t11)
        t33 = self.text3(t22)
        t44 = self.text4(t33)
        t55 = self.text5(t44)
        t66 = self.text6(t55)
        
        #Generator
        if s == 1:           ### s=True --> resconstruction
          out = self.g1(s3, t6)
          out = self.g2(out, t6)
          out = torch.cat([out, s1], 1)
          out = self.g3(out, t6)
          out = torch.cat([out, in4], 1)
          out = self.g4(out, t6)
          out = torch.cat([out, in3], 1)
          out = self.g5(out, t6)
          out = torch.cat([out, in2], 1)
          out = self.g6(out, t6)
          out = torch.cat([out, in1], 1)
          out = self.to_rgb(out)
          # print("66666666666666666666666666666666666666666")
        elif s == 0:                 ###s=False --> distortion_img
          out = self.g1(s3, t66)
          out = self.g2(out, t66)
          out = torch.cat([out, s1], 1)
          out = self.g3(out, t66)
          out = torch.cat([out, in4], 1)
          out = self.g4(out, t66)
          out = torch.cat([out, in3], 1)
          out = self.g5(out, t66)
          out = torch.cat([out, in2], 1)
          out = self.g6(out, t66)
          out = torch.cat([out, in1], 1)
          out = self.to_rgb(out)
          # print("555555555555555555555555555555555555555")
        return out



class Discriminator(nn.Module):
    def __init__(self, size, channel_multiplier=1):
        super().__init__()

        channels = {
            4: 512,
            8: 512,
            16: 512,
            32: 512,
            64: 256 * channel_multiplier,
            128: 128 * channel_multiplier,
            256: 64 * channel_multiplier,
            512: 32 * channel_multiplier,
            1024: 16 * channel_multiplier,
        }

        convs = [ConvLayer(3, channels[size], 1)]

        log_size = int(math.log(size, 2))

        in_channel = channels[size]

        for i in range(log_size, 2, -1):
            out_channel = channels[2 ** (i - 1)]

            convs.append(ResBlock(in_channel, out_channel, downsample=True))

            in_channel = out_channel

        self.convs = nn.Sequential(*convs)

        self.final_conv = ConvLayer(in_channel, channels[4], 3)
        self.final_linear = nn.Sequential(
            EqualLinear(channels[4] * 4 * 4, channels[4], activation="fused_lrelu"),
            EqualLinear(channels[4], 1),
        )

    def forward(self, input):
        out = self.convs(input)
        out = self.final_conv(out)

        out = out.view(out.shape[0], -1)
        out = self.final_linear(out)

        return out


class CooccurDiscriminator(nn.Module):
    def __init__(self, channel, size=256):
        super().__init__()

        encoder = [ConvLayer(3, channel, 1)]

        ch_multiplier = (2, 4, 8, 12, 12, 24)
        downsample = (True, True, True, True, True, False)
        if size==1024:
            downsample = (True, True, True, True, True, True)
        in_ch = channel
        for ch_mul, down in zip(ch_multiplier, downsample):
            encoder.append(ResBlock(in_ch, channel * ch_mul, down))
            in_ch = channel * ch_mul

        if size > 511:
            k_size = 3
            feat_size = 2 * 2

        else:
            k_size = 3
            feat_size = 2 * 2 

        encoder.append(ConvLayer(in_ch, channel * 12, k_size, padding="valid"))

        self.encoder = nn.Sequential(*encoder)

        self.linear = nn.Sequential(
            EqualLinear(
                channel * 12 * 2 * feat_size, channel * 32, activation="fused_lrelu"
            ),
            EqualLinear(channel * 32, channel * 32, activation="fused_lrelu"),
            EqualLinear(channel * 32, channel * 16, activation="fused_lrelu"),
            EqualLinear(channel * 16, 1),
        )

    def forward(self, input, reference=None, ref_batch=None, ref_input=None):
        out_input = self.encoder(input)
        if ref_input is None:
            ref_input = self.encoder(reference)
            _, channel, height, width = ref_input.shape
            ref_input = ref_input.view(-1, ref_batch, channel, height, width)
            ref_input = ref_input.mean(1)

        out = torch.cat((out_input, ref_input), 1)
        out = torch.flatten(out, 1)
        out = self.linear(out)

        return out, ref_input
