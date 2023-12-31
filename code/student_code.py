import numpy as np
import torch
import torch.nn as nn
import torch.nn.functional as F
from torch.autograd import Function
from torch.nn.modules.module import Module
from torch.nn.functional import fold, unfold
from torchvision.utils import make_grid
import math

from utils import resize_image
import custom_transforms as transforms
from custom_blocks import PatchEmbed, TransformerBlock, trunc_normal_


#################################################################################
# You will need to fill in the missing code in this file
#################################################################################


#################################################################################
# Part I: Understanding Convolutions
#################################################################################
class CustomConv2DFunction(Function):
    @staticmethod
    def forward(ctx, input_feats, weight, bias, stride=1, padding=0):
        """
        Forward propagation of convolution operation.
        We only consider square filters with equal stride/padding in width and height!

        Args:
          input_feats: input feature map of size N * C_i * H * W
          weight: filter weight of size C_o * C_i * K * K
          bias: (optional) filter bias of size C_o
          stride: (int, optional) stride for the convolution. Default: 1
          padding: (int, optional) Zero-padding added to both sides of the input. Default: 0

        Outputs:
          output: responses of the convolution  w*x+b

        """
        # sanity check
        assert weight.size(2) == weight.size(3)
        assert input_feats.size(1) == weight.size(1)
        assert isinstance(stride, int) and (stride > 0)
        assert isinstance(padding, int) and (padding >= 0)

        # save the conv params
        kernel_size = weight.size(2)
        ctx.stride = stride
        ctx.padding = padding
        ctx.input_height = input_feats.size(2)
        ctx.input_width = input_feats.size(3)

        # make sure this is a valid convolution
        assert kernel_size <= (input_feats.size(2) + 2 * padding)
        assert kernel_size <= (input_feats.size(3) + 2 * padding)

        #################################################################################
        # Fill in the code here
        #################################################################################

        # Extract necessary dimensions
        N, H = input_feats.size(0), input_feats.size(2)
        C_o = weight.size(0)

        # input_unfolded.shape = (N, C_i * K * K, H_o * W_o)
        # unfold([[[[1, 2], [3, 4]], [[5, 6], [7, 8]]]]) = [[[[1, ..., 8]]]]
        # weight_unfolded.shape = (C_o, C_i * K * K)
        input_unfolded = unfold(input_feats, kernel_size, padding=padding, stride=stride)
        weight_unfolded = weight.view(C_o, -1)

        # output_unfolded.shape = (N, C_o, H_o * W_o)
        output_unfolded = weight_unfolded @ input_unfolded
        # Broadcast bias along
        # - the output grid
        # - the input channels
        # Trivially, the batch dimension too but that's the case for all parameters
        if bias is not None:
            output_unfolded += bias.view(-1, 1)
        # Fold the output to grid shape
        # output.shape = (N, C_o, H_o, W_o)
        H_o = (H + 2 * padding - kernel_size) // stride + 1
        # Use reshape instead of view to avoid in-place modification errors
        output = output_unfolded.view(N, C_o, H_o, -1).clone()

        # save for backward (you need to save the unfolded tensor into ctx)
        # ctx.save_for_backward(your_vars, weight, bias)
        ctx.save_for_backward(input_unfolded, weight, bias)

        return output

    @staticmethod
    def backward(ctx, grad_output):
        """
        Backward propagation of convolution operation

        Args:
          grad_output: gradients of the outputs

        Outputs:
          grad_input: gradients of the input features
          grad_weight: gradients of the convolution weight
          grad_bias: gradients of the bias term

        """
        # unpack tensors and initialize the grads
        # your_vars, weight, bias = ctx.saved_tensors
        input_unfolded, weight, bias = ctx.saved_tensors
        grad_input = grad_weight = grad_bias = None

        # recover the conv params
        kernel_size = weight.size(2)
        stride = ctx.stride
        padding = ctx.padding
        input_height = ctx.input_height
        input_width = ctx.input_width

        #################################################################################
        # Fill in the code here
        #################################################################################
        # compute the gradients w.r.t. input and params

        # grad_output_unfolded.shape = (N, C_o, H_o * W_o)
        grad_output_unfolded = grad_output.view(grad_output.size(0), grad_output.size(1), -1)

        # Compute input gradients
        if ctx.needs_input_grad[0]:
            # weight_unfolded.shape = (C_o, C_i * K * K)
            # grad_input_unfolded.shape = (N, C_i * K * K, H_o * W_o)
            # grad_input.shape = (N, C_i, H, W)
            weight_unfolded = weight.view(weight.size(0), -1)
            grad_input_unfolded = weight_unfolded.T @ grad_output_unfolded
            grad_input = fold(grad_input_unfolded, (input_height, input_width), kernel_size, padding=padding, stride=stride)

        # Compute weight gradients
        if ctx.needs_input_grad[1]:
            # input_transpose.shape = (N, H_o * W_o, C_i * K * K)
            # grad_weight_unfolded.shape = (C_o, C_i * K * K)
            # grad_weight.shape = (C_o, C_i, K, K)
            input_transpose = torch.transpose(input_unfolded, 1, 2)
            # Sum over the batch dimension. Additive contribution.
            grad_weight_unfolded = torch.sum(grad_output_unfolded @ input_transpose, dim=0)
            # Use reshape, not view, to avoid in-place errors
            grad_weight = grad_weight_unfolded.view(weight.size(0), -1, kernel_size, kernel_size)

        if bias is not None and ctx.needs_input_grad[2]:
            # compute the gradients w.r.t. bias (if any)
            grad_bias = grad_output.sum((0, 2, 3))

        return grad_input, grad_weight, grad_bias, None, None


custom_conv2d = CustomConv2DFunction.apply


class CustomConv2d(Module):
    """
    The same interface as torch.nn.Conv2D
    """

    def __init__(
        self,
        in_channels,
        out_channels,
        kernel_size,
        stride=1,
        padding=0,
        dilation=1,
        groups=1,
        bias=True,
    ):
        super(CustomConv2d, self).__init__()
        assert isinstance(kernel_size, int), "We only support squared filters"
        assert isinstance(stride, int), "We only support equal stride"
        assert isinstance(padding, int), "We only support equal padding"
        self.in_channels = in_channels
        self.out_channels = out_channels
        self.kernel_size = kernel_size
        self.stride = stride
        self.padding = padding

        # not used (for compatibility)
        self.dilation = dilation
        self.groups = groups

        # register weight and bias as parameters
        self.weight = nn.Parameter(
            torch.Tensor(out_channels, in_channels, kernel_size, kernel_size)
        )
        if bias:
            self.bias = nn.Parameter(torch.Tensor(out_channels))
        else:
            self.register_parameter("bias", None)
        self.reset_parameters()

    def reset_parameters(self):
        # initialization using Kaiming uniform
        nn.init.kaiming_uniform_(self.weight, a=math.sqrt(5))
        if self.bias is not None:
            fan_in, _ = nn.init._calculate_fan_in_and_fan_out(self.weight)
            bound = 1 / math.sqrt(fan_in)
            nn.init.uniform_(self.bias, -bound, bound)

    def forward(self, input):
        # call our custom conv2d op
        return custom_conv2d(input, self.weight, self.bias, self.stride, self.padding)

    def extra_repr(self):
        s = (
            "{in_channels}, {out_channels}, kernel_size={kernel_size}"
            ", stride={stride}, padding={padding}"
        )
        if self.bias is None:
            s += ", bias=False"
        return s.format(**self.__dict__)


#################################################################################
# Part II: Design and train a network
#################################################################################
class SimpleNet(nn.Module):
    # a simple CNN for image classifcation
    def __init__(self, conv_op=nn.Conv2d, num_classes=100, attack = False):
        super(SimpleNet, self).__init__()
        # you can start from here and create a better model
        self.features = nn.Sequential(
            # conv1 block: conv 7x7
            conv_op(3, 64, kernel_size=7, stride=2, padding=3),
            nn.ReLU(inplace=True),
            # max pooling 1/2
            nn.MaxPool2d(kernel_size=3, stride=2, padding=1),
            # conv2 block: simple bottleneck
            conv_op(64, 64, kernel_size=1, stride=1, padding=0),
            nn.ReLU(inplace=True),
            conv_op(64, 64, kernel_size=3, stride=1, padding=1),
            nn.ReLU(inplace=True),
            conv_op(64, 256, kernel_size=1, stride=1, padding=0),
            nn.ReLU(inplace=True),
            # max pooling 1/2
            nn.MaxPool2d(kernel_size=3, stride=2, padding=1),
            # conv3 block: simple bottleneck
            conv_op(256, 128, kernel_size=1, stride=1, padding=0),
            nn.ReLU(inplace=True),
            conv_op(128, 128, kernel_size=3, stride=1, padding=1),
            nn.ReLU(inplace=True),
            conv_op(128, 512, kernel_size=1, stride=1, padding=0),
            nn.ReLU(inplace=True),
        )
        # global avg pooling + FC
        self.avgpool = nn.AdaptiveAvgPool2d((1, 1))
        self.fc = nn.Linear(512, num_classes)
        self.attack = attack

    def reset_parameters(self):
        # init all params
        for m in self.modules():
            if isinstance(m, nn.Conv2d):
                nn.init.kaiming_normal_(m.weight, mode="fan_out", nonlinearity="relu")
                if m.bias is not None:
                    nn.init.consintat_(m.bias, 0.0)
            elif isinstance(m, nn.BatchNorm2d):
                nn.init.constant_(m.weight, 1.0)
                nn.init.constant_(m.bias, 0.0)

    def forward(self, x):
        # you can implement adversarial training here
        if self.training and self.attack:
            attacker = PGDAttack(F.cross_entropy, num_steps=5, step_size=0.01, epsilon=0.1)
            x = attacker.perturb(self, x)
        
        x = self.features(x)
        x = self.avgpool(x)
        x = x.view(x.size(0), -1)
        x = self.fc(x)
        #print(x.argmin(dim=1))
        return x
    

class CustomNet(nn.Module):
    # a simple CNN for image classifcation
    def __init__(self, conv_op=nn.Conv2d, num_classes=100, res_depth = 4):
        super(CustomNet, self).__init__()
        # you can start from here and create a better model

        print(f"You are in customnet with depth {res_depth}!")

        self.features1 = nn.Sequential(
            # conv1 block: conv 7x7
            conv_op(3, 64, kernel_size=7, stride=2, padding=3),
            nn.ReLU(inplace=True),
            # max pooling 1/2
            nn.MaxPool2d(kernel_size=3, stride=2, padding=1),

            nn.BatchNorm2d(64),

            # conv2 block: simple bottleneck
            conv_op(64, 64, kernel_size=1, stride=1, padding=0),
            nn.ReLU(inplace=True),
            conv_op(64, 64, kernel_size=3, stride=1, padding=1),
            nn.ReLU(inplace=True),
            conv_op(64, 256, kernel_size=1, stride=1, padding=0),
            nn.ReLU(inplace=True),
            # max pooling 1/2
            nn.MaxPool2d(kernel_size=3, stride=2, padding=1),

            nn.BatchNorm2d(256),
        )

        resblock = nn.Sequential(
            # conv2 block: simple bottleneck
            conv_op(256, 64, kernel_size=1, stride=1, padding=0),
            nn.ReLU(inplace=True),
            conv_op(64, 64, kernel_size=3, stride=1, padding=1),
            nn.ReLU(inplace=True),
            conv_op(64, 256, kernel_size=1, stride=1, padding=0),
            nn.ReLU(inplace=True),

            nn.BatchNorm2d(256),
        )

        self.reslist = nn.ModuleList()
        self.res_depth = res_depth

        for i in range(res_depth):
            self.reslist.append(resblock)


        self.features2 = nn.Sequential(
            # conv3 block: simple bottleneck
            conv_op(256, 128, kernel_size=1, stride=1, padding=0),
            nn.ReLU(inplace=True),
            conv_op(128, 128, kernel_size=3, stride=1, padding=1),
            nn.ReLU(inplace=True),
            conv_op(128, 512, kernel_size=1, stride=1, padding=0),
            nn.ReLU(inplace=True),
        )

        # global avg pooling + FC
        self.avgpool = nn.AdaptiveAvgPool2d((1, 1))
        #self.fc = nn.Linear(512, num_classes)
        self.fc1 = nn.Linear(512, 256)
        self.relu = nn.ReLU()
        self.batchnorm = nn.BatchNorm1d(256)
        self.fc2 = nn.Linear(256, num_classes)

    def reset_parameters(self):
        # init all params
        for m in self.modules():
            if isinstance(m, nn.Conv2d):
                nn.init.kaiming_normal_(m.weight, mode="fan_out", nonlinearity="relu")
                if m.bias is not None:
                    nn.init.consintat_(m.bias, 0.0)
            elif isinstance(m, nn.BatchNorm2d):
                nn.init.constant_(m.weight, 1.0)
                nn.init.constant_(m.bias, 0.0)

    def forward(self, x):
        # you can implement adversarial training here
        # if self.training:
        #   # generate adversarial sample based on x
        x = self.features1(x)

        for i in range(self.res_depth):
            residuals = x
            x = self.reslist[i](x)
            x = x + residuals

        x = self.features2(x)
        x = self.avgpool(x)
        x = x.view(x.size(0), -1)
        x = self.fc1(x)
        x = self.relu(x)
        x = self.batchnorm(x)
        x = self.fc2(x)

        return x


class SimpleViT(nn.Module):
    """
    This module implements Vision Transformer (ViT) backbone in
    "Exploring Plain Vision Transformer Backbones for Object Detection",
    https://arxiv.org/abs/2203.16527
    """

    def __init__(
        self,
        img_size=128,
        num_classes=100,
        patch_size=16,
        in_chans=3,
        embed_dim=192,
        depth=4,
        num_heads=4,
        mlp_ratio=4.0,
        qkv_bias=True,
        drop_path_rate=0.1,
        norm_layer=nn.LayerNorm,
        act_layer=nn.GELU,
        use_abs_pos=True,
        window_size=4,
        window_block_indexes=(0, 2),
    ):
        """
        Args:
            img_size (int): Input image size.
            num_classes (int): Number of object categories
            patch_size (int): Patch size.
            in_chans (int): Number of input image channels.
            embed_dim (int): Patch embedding dimension.
            depth (int): Depth of ViT.
            num_heads (int): Number of attention heads in each ViT block.
            mlp_ratio (float): Ratio of mlp hidden dim to embedding dim.
            qkv_bias (bool): If True, add a learnable bias to query, key, value.
            drop_path_rate (float): Stochastic depth rate.
            norm_layer (nn.Module): Normalization layer.
            act_layer (nn.Module): Activation layer.
            use_abs_pos (bool): If True, use absolute positional embeddings.
            window_size (int): Window size for local attention blocks.
            window_block_indexes (list): Indexes for blocks using local attention.
                Local window attention allows more efficient computation, and can be
                coupled with standard global attention.
                E.g., [0, 2] indicates the first and the third blocks will use
                local window attention, while other block use standard attention.

        Feel free to modify the default parameters here.
        """
        super(SimpleViT, self).__init__()

        if use_abs_pos:
            # Initialize absolute positional embedding with image size
            # The embedding is learned from data

            self.pos_embed = nn.Parameter(
                torch.zeros(
                    1, ((img_size // patch_size) * (img_size // patch_size)), embed_dim
                )
            )

        else:
            self.pos_embed = None

        # stochastic depth decay rule
        dpr = [x.item() for x in torch.linspace(0, drop_path_rate, depth)]

        ########################################################################
        # Fill in the code here

        # Define patch embedding layer
        self.patch_embed = PatchEmbed(
            kernel_size=(patch_size, patch_size),
            stride=(patch_size, patch_size),
            in_chans=in_chans,
            embed_dim=embed_dim,
        )

        # Define transformer layers that make up the transformer enconder
        self.transformer_blocks = nn.ModuleList()

        for i in range(depth):
            tblock = TransformerBlock(
                dim=embed_dim,
                num_heads=num_heads,
                mlp_ratio=mlp_ratio,
                qkv_bias=qkv_bias,
                drop_path=dpr[i],
                norm_layer=norm_layer,
                act_layer=act_layer,
                window_size=window_size if i in window_block_indexes else 0,
            )

            self.transformer_blocks.append(tblock)

        # Define final head and normalization for logit outputs
        self.norm = nn.LayerNorm(embed_dim)
        self.head = nn.Linear(embed_dim, num_classes, bias=True)


        ########################################################################
        # the implementation shall start from embedding patches,
        # followed by some transformer blocks

        if self.pos_embed is not None:
            trunc_normal_(self.pos_embed, std=0.02)

        self.apply(self._init_weights)
        # add any necessary weight initialization here

    def _init_weights(self, m):
        if isinstance(m, nn.Linear):
            trunc_normal_(m.weight, std=0.02)
            if isinstance(m, nn.Linear) and m.bias is not None:
                nn.init.constant_(m.bias, 0)
        elif isinstance(m, nn.LayerNorm):
            nn.init.constant_(m.bias, 0)
            nn.init.constant_(m.weight, 1.0)

    def forward(self, x):
        ########################################################################
        # Fill in the code here

        # Do patch embedding
        x = self.patch_embed(x)

        # Add in absolute position embeddings
        if self.pos_embed is not None:
            # First, transform absolute position embeddings into desired shape: (1, H, W, C)
            h, w = (x.shape[1], x.shape[2]) # h * w = total number of patches
            abs_pos = self.pos_embed
            total_num_patches = abs_pos.shape[1] 
            patches_per_side = int(math.sqrt(total_num_patches))
            assert patches_per_side * patches_per_side == total_num_patches

            # Handle the case if h/w do not equal patches_per_side
            if patches_per_side != h or patches_per_side != w:
                new_abs_pos = F.interpolate(
                    abs_pos.reshape(1, patches_per_side, patches_per_side, -1).permute(0, 3, 1, 2),
                    size=(h,w),
                    mode="bicubic",
                    align_corners=False,
                )

                abs_pos = new_abs_pos.permute(0, 2, 3, 1)
            else:
                abs_pos = abs_pos.reshape(1, h, w, -1)

            # Add pos embeddings to x
            x = x + abs_pos

        # Apply transformation blocks
        for tblock in self.transformer_blocks:
            x = tblock(x)

        # Apply final normalization/head
        # Collapse the patches, reorder into (batch_size, embed_dim, #patches)
        x = x.reshape(x.shape[0], -1, x.shape[-1]).permute(0, 2, 1)
        # Aggregate the patch features by using global average
        x = x.mean(2)
        # Apply normalization layer
        x = self.norm(x)
        # Apply head layer
        x = self.head(x)

        # Now have output logits of form (batch_size, #classes). Can get predicted label by doing argmax...

        ########################################################################
        return x



# change this to your model!
default_cnn_model = CustomNet
default_vit_model = SimpleViT

# define data augmentation used for training, you can tweak things if you want
def get_train_transforms(normalize):
    train_transforms = []
    train_transforms.append(transforms.Scale(144))
    train_transforms.append(transforms.RandomHorizontalFlip())
    train_transforms.append(transforms.RandomColor(0.15))
    train_transforms.append(transforms.RandomRotate(15))
    train_transforms.append(transforms.RandomSizedCrop(128))
    train_transforms.append(transforms.ToTensor())
    train_transforms.append(normalize)
    train_transforms = transforms.Compose(train_transforms)
    return train_transforms


# define data augmentation used for validation, you can tweak things if you want
def get_val_transforms(normalize):
    val_transforms = []
    val_transforms.append(transforms.Scale(144))
    val_transforms.append(transforms.CenterCrop(128))
    val_transforms.append(transforms.ToTensor())
    val_transforms.append(normalize)
    val_transforms = transforms.Compose(val_transforms)
    return val_transforms


#################################################################################
# Part III: Adversarial samples and Attention
#################################################################################
class PGDAttack(object):
    def __init__(self, loss_fn, num_steps=10, step_size=0.01, epsilon=0.1):
        """
        Attack a network by Project Gradient Descent. The attacker performs
        k steps of gradient descent of step size a, while always staying
        within the range of epsilon (under l infinity norm) from the input image.

        Args:
          loss_fn: loss function used for the attack
          num_steps: (int) number of steps for PGD
          step_size: (float) step size of PGD (i.e., alpha in our lecture)
          epsilon: (float) the range of acceptable samples
                   for our normalization, 0.1 ~ 6 pixel levels
        """
        self.loss_fn = loss_fn
        self.num_steps = num_steps
        self.step_size = step_size
        self.epsilon = epsilon

    def perturb(self, model, input):
        """
        Given input image X (torch tensor), return an adversarial sample
        (torch tensor) using PGD of the least confident label.

        See https://openreview.net/pdf?id=rJzIBfZAb

        Args:
          model: (nn.module) network to attack
          input: (torch tensor) input image of size N * C * H * W

        Outputs:
          output: (torch tensor) an adversarial sample of the given network
        """

        # clone the input tensor and disable the gradients
        output = input.clone()
        input.requires_grad = False

        # set model mode to eval so forward function does not loop infinitely
        training = model.training
        if training:
            model.eval()
        
        for i in range(self.num_steps):
            output.requires_grad_() # require gradients
            
            if output.grad is not None:
                output.grad.zero_() 

            # Forward pass
            with torch.enable_grad():
                logits = model(output)

                pred = logits.argmin(dim=1)
                loss = self.loss_fn(logits, pred)

            # Get the gradients and store them externally from model
            gradients = torch.autograd.grad(loss, [output])[0]

            # Create adversarial sample, clamp values
            output = output.detach() - self.step_size * torch.sign(gradients.detach())
            output = torch.max(torch.min(output.data, input + self.epsilon), input - self.epsilon)

        # Set model back to training if necessary
        if training:
            model.train()

        return output


default_attack = PGDAttack


class GradAttention(object):
    def __init__(self, loss_fn):
        """
        Visualize a network's decision using gradients

        Args:
          loss_fn: loss function used for the attack
        """
        self.loss_fn = loss_fn

    def explain(self, model, input):
        """
        Given input image X (torch tensor), return a saliency map
        (torch tensor) by computing the max of abs values of the gradients
        given by the predicted label

        See https://arxiv.org/pdf/1312.6034.pdf

        Args:
          model: (nn.module) network to attack
          input: (torch tensor) input image of size N * C * H * W

        Outputs:
          output: (torch tensor) a saliency map of size N * 1 * H * W
        """
        input.requires_grad = True

        if input.grad is not None:
            input.grad.zero_()

        for params in model.parameters():
            params.requires_grad = False
        
        model.eval()

        # Forward pass
        output = model(input) 
        pred = output.argmax(1)
        loss = self.loss_fn(output, pred)

        # Backward pass
        loss.backward() 

        grads = input.grad.data.abs()
       
        # Take maximum across channels
        saliency, _ = grads.max(1)
        saliency = saliency.unsqueeze(1)
        return saliency


default_attention = GradAttention


def vis_grad_attention(input, vis_alpha=2.0, n_rows=10, vis_output=None):
    """
    Given input image X (torch tensor) and a saliency map
    (torch tensor), compose the visualziations

    Args:
      input: (torch tensor) input image of size N * C * H * W
      output: (torch tensor) input map of size N * 1 * H * W

    Outputs:
      output: (torch tensor) visualizations of size 3 * HH * WW
    """
    # concat all images into a big picture
    input_imgs = make_grid(input.cpu(), nrow=n_rows, normalize=True)
    if vis_output is not None:
        output_maps = make_grid(vis_output.cpu(), nrow=n_rows, normalize=True)

        # somewhat awkward in PyTorch
        # add attention to R channel
        mask = torch.zeros_like(output_maps[0, :, :]) + 0.5
        mask = output_maps[0, :, :] > vis_alpha * output_maps[0, :, :].mean()
        mask = mask.float()
        input_imgs[0, :, :] = torch.max(input_imgs[0, :, :], mask)
    output = input_imgs
    return output


default_visfunction = vis_grad_attention
