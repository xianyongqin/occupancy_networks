import torch.nn as nn
import torch
import torch.nn.functional as F
from im2mesh.onet_multi_layers_predict.models import resnet
from im2mesh.common import normalize_imagenet
import im2mesh.common as common

def kmax_pooling(x, dim, k):
    index = x.topk(k, dim=dim)[1].sort(dim=dim)[0]
    return x.gather(dim, index)

class Resnet18_Full(nn.Module):
    r''' ResNet-18 encoder network for image input.
    Args:
        c_dim (int): output dimension of the latent embedding
        normalize (bool): whether the input images should be normalized
        use_linear (bool): whether a final linear layer should be used
    '''

    def __init__(self, c_dim, normalize=True):
        super().__init__()
        self.normalize = normalize
        self.features = resnet.resnet18(pretrained=True)

        if c_dim != 512:
            self.fc3 = nn.Linear(512, c_dim)
            self.fc2 = nn.Linear(512, c_dim)
            self.fc1 = nn.Linear(512, c_dim)
        else:
            self.fc3 = nn.Sequential()
            self.fc2 = nn.Sequential()
            self.fc1 = nn.Sequential()

        self.f1_fc = nn.Linear(1568,512)
        self.f2_fc = nn.Linear(1568,512)

    def forward(self, x):
        if self.normalize:
            x = normalize_imagenet(x)
        f3,f2,f1 = self.features(x)
        # f3: 512 f2: 256 * 14 * 14 f1: 128 * 28 * 28
  
        # full kmax pooling
        f1 = kmax_pooling(f1,1,2)
        f1 = f1.view(f1.size(0), -1)
        f1 = self.f1_fc(f1)

        f2 = kmax_pooling(f2,1,8)
        f2 = f2.view(f2.size(0), -1)
        f2 = self.f2_fc(f2)

        f3 = self.fc3(f3)
        f2 = self.fc2(f2)
        f1 = self.fc1(f1)
        return f3, f2, f1


class Resnet18_Local(nn.Module):
    r''' ResNet-18 encoder network for image input.
    Args:
        c_dim (int): output dimension of the latent embedding
        normalize (bool): whether the input images should be normalized
        use_linear (bool): whether a final linear layer should be used
    '''

    def __init__(self, c_dim, feature_map_dim=64 ,normalize=True):
        super().__init__()
        self.normalize = normalize
        self.features = resnet.resnet18(pretrained=True)

        if c_dim != 512:
            self.fc3 = nn.Linear(512, c_dim)
        else:
            self.fc3 = nn.Sequential()

        self.feature_map_dim = feature_map_dim
        self.f2_conv = nn.Conv1d(256, self.feature_map_dim ,1)
        self.f1_conv = nn.Conv1d(128, self.feature_map_dim ,1)


    def forward(self, x, pts, world_mat, camera_mat):
        if self.normalize:
            x = normalize_imagenet(x)
        f3,f2,f1 = self.features(x)
        # f3: 512 f2: 256 * 14 * 14 f1: 128 * 28 * 28

        # position
        pts = common.transform_points(pts, world_mat)
        points_img = common.project_to_camera(pts, camera_mat)
        points_img = points_img.unsqueeze(1)

        f2 = F.relu(f2)
        f2 = F.grid_sample(f2, points_img)
        f2 = f2.squeeze(2)
        f2 = self.f2_conv(f2)
        f2 = f2.transpose(1, 2)
        # f2 : batch * n_pts * fmap_dim

        f1 = F.relu(f1)
        f1 = F.grid_sample(f1, points_img)
        f1 = f1.squeeze(2)
        f1 = self.f1_conv(f1)
        f1 = f1.transpose(1, 2)
        # f1 : batch * n_pts * fmap_dim

        f3 = self.fc3(f3)
        return f3, f2, f1

    def encode_first_step(self, x):
        if self.normalize:
            x = normalize_imagenet(x)
        f3,f2,f1 = self.features(x)

        return f3, f2, f1
    
    def encode_second_step(self, f3, f2, f1, pts, world_mat, camera_mat):
        pts = common.transform_points(pts, world_mat)
        points_img = common.project_to_camera(pts, camera_mat)
        points_img = points_img.unsqueeze(1)

        f2 = F.relu(f2)
        f2 = F.grid_sample(f2, points_img)
        f2 = f2.squeeze(2)
        f2 = self.f2_conv(f2)
        f2 = f2.transpose(1, 2)
        # f2 : batch * n_pts * fmap_dim

        f1 = F.relu(f1)
        f1 = F.grid_sample(f1, points_img)
        f1 = f1.squeeze(2)
        f1 = self.f1_conv(f1)
        f1 = f1.transpose(1, 2)
        # f1 : batch * n_pts * fmap_dim

        f3 = self.fc3(f3)
        return f3, f2, f1