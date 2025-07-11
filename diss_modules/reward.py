from abc import ABC, abstractmethod
import torch
import numpy as np
from absl.logging import flush
from torch.onnx.symbolic_opset9 import tensor
from torchvision import transforms
import inference  # from AdaFace
from face_alignment import align, mtcnn  # from AdaFace
from pathlib import Path
from PIL import Image
import torch.nn.functional as F
from typing import Tuple, List
import clip
import os
import ImageReward as RM

__REWARD_METHOD__ = {}


def register_reward_method(name: str):
    def wrapper(cls):
        if __REWARD_METHOD__.get(name, None):
            raise NameError(f"Name {name} is already registered!")
        __REWARD_METHOD__[name] = cls
        return cls

    return wrapper


def get_reward_method(name: str, **kwargs):
    if __REWARD_METHOD__.get(name, None) is None:
        raise NameError(f"Name {name} is not defined!")
    return __REWARD_METHOD__[name](**kwargs)


class Reward(ABC):

    def __init__(self, **kwargs):
        """
        Optional constructor for reward classes. Accepts arbitrary keyword arguments
        for flexibility and downstream configuration.
        """
        pass

    @abstractmethod
    def get_reward(self, particles, **kwargs) -> torch.Tensor:
        """
        Compute and return the reward signal given inputs.

        Args:
            particles: The particles that you want to find the reward for
            **kwargs: Task-specific keyword arguments such as 'images', 'text', etc.

        Returns:
            A torch.Tensor representing the reward(s).
        """
        pass

    @abstractmethod
    def get_gradients(self, particles, **kwargs) -> torch.Tensor:
        """
        Compute and return the gradient of the difference of the embedding
        of particles with the embedding of given information.

        Args:
            particles: The particles that you want to find the gradient with respect to
            **kwargs: Task-specific keyword arguments

        Returns:
            A torch.Tensor representing the reward(s).
        """
        pass

    @abstractmethod
    def set_side_info(self, **kwargs):
        pass


@register_reward_method('adaface')
class AdaFaceReward(Reward):
    """
    Reward function based on AdaFace facial embeddings.

    This class computes the similarity between a generated face image and a reference
    face (additional image of the same person) using embeddings from a pretrained AdaFace model.

    The reward can be used for guiding diffusion models in tasks like face reconstruction,
    identity-preserving generation, and image alignment.

    Attributes:
        files (List[Path]): List of all image file paths in the dataset directory.
        model: Pretrained AdaFace embedding model.
        side_info (torch.Tensor): Ground-truth face embedding.
        device (str): Computation device, e.g., 'cuda' or 'cpu'.
        mtcnn_model: Face detector and aligner (MTCNN).
        res (int): Target image resolution for preprocessing.
    """
    ADAFACE_PATH = '../../third_party/AdaFace'

    def __init__(self, data_path: str = '../../data/additional_images', pretrained_model: str = 'ir_50',
                 resolution: int = 256, device: str = 'cuda:0', scale=1, **kwargs):
        """
        Initializes the AdaFaceReward class.

        Args:
            data_path (str): Path to the directory containing face images.
            pretrained_model (str): Name of the pretrained AdaFace model to load.
            resolution (int): Target resolution to resize and center crop images.
            device (str): Torch device for inference, default is 'cuda:0'.
            **kwargs: Additional unused keyword arguments.
        """
        super().__init__(**kwargs)
        file_types = ['*.jpg', '*.JPG', '*.jpeg', '*.JPEG', '*.png', '*.PNG']
        self.device = device
        self.files = sorted([f for ft in file_types for f in Path(data_path).rglob(ft)])

        original_dir = os.getcwd()
        os.chdir(self.ADAFACE_PATH)
        self.model = inference.load_pretrained_model(pretrained_model).to(self.device)
        os.chdir(original_dir)

        self.side_info = None
        self.mtcnn_model = mtcnn.MTCNN(device=self.device, crop_size=(112, 112))
        self.res = resolution
        self.scale = scale
        self.name = 'adaface'
        self.embed_size = 512

    def get_reward(self, images: torch.Tensor, **kwargs) -> torch.Tensor:
        """
        Computes the negative L2 distance between the embeddings of given images
        and the stored ground-truth embedding.

        Args:
            images (torch.Tensor): Input batch of images (B, C, H, W) in [-1, 1].

        Returns:
            torch.Tensor: A tensor of shape B containing reward values.
        """
        embed = self._embeddings(images)
        return - torch.norm(self.side_info - embed, dim=1)

    def set_side_info(self, index: int) -> None:
        """
        Sets the ground-truth embedding by loading and embedding the additional image
        at the given index in the dataset.

        Args:
            index (int): Index of the reference image in the dataset list.
        """
        # Load and preprocess image
        img = Image.open(self.files[index]).convert("RGB")
        trans = transforms.Compose([
            transforms.ToTensor(),
            transforms.Resize(self.res),
            transforms.CenterCrop(self.res)
        ])
        img_tensor = (trans(img) * 2 - 1).to(self.device)
        if img_tensor.shape[0] == 1:
            img_tensor = img_tensor.expand(3, -1, -1)

        # Set side information
        self.side_info = self._embeddings(img_tensor.unsqueeze(0)).detach()

    def _embeddings(self, tensor_images: torch.Tensor) -> torch.Tensor:
        """
        Computes AdaFace embeddings for a batch of images.

        Each image is aligned using MTCNN and passed through the pretrained model.

        Args:
            tensor_images (torch.Tensor): Batch of images (B, C, H, W) in [-1, 1].

        Returns:
            torch.Tensor: A tensor of shape (B, D) with D-dimensional embeddings.
        """
        tensor_images = ((tensor_images + 1) / 2 * 255).clamp(0, 255).byte()
        to_pil = transforms.ToPILImage()

        aligned_images, failed_indices = [], []
        for i in range(tensor_images.size(0)):
            try:
                img = to_pil(tensor_images[i])
                aligned = align.get_aligned_face('', rgb_pil_image=img)
                aligned_images.append(inference.to_input(aligned).to(self.device))
            except Exception as e:
                print('No face detected in x0 at index {0}, adding fallback embedding.'.format(i), flush=True)
                failed_indices.append(i)
                aligned_images.append(torch.randn((1, 3, 112, 112), device=self.device))

        batch_input = torch.cat(aligned_images, dim=0)  # Assuming dim=0 is batch
        embeddings, _ = self.model(batch_input)
        if failed_indices:
            fallback = torch.ones((len(failed_indices), self.embed_size), device=embeddings.device) * 1e3
            embeddings[torch.tensor(failed_indices, device=embeddings.device)] = fallback

        return embeddings

    def get_gradients(self, images: torch.Tensor, **kwargs) -> Tuple[torch.Tensor, torch.Tensor]:

        B, C, H, W = images.shape
        images = images.clone().detach().requires_grad_(True)

        # ------------------------------------------------------------------
        # 1. Detect faces (no grad)
        # ------------------------------------------------------------------
        to_pil = transforms.ToPILImage()
        bboxes, failed = [], []

        for i in range(B):
            img_uint8 = ((images[i].detach() + 1) * 127.5).clamp(0, 255).byte().cpu()
            pil_img = to_pil(img_uint8)
            boxes, _ = self.mtcnn_model.align_multi(pil_img, limit=1)

            if len(boxes) == 0:  # fallback → use whole frame
                failed.append(i)
                bboxes.append(None)
            else:
                x1, y1, x2, y2 = boxes[0][:4].astype(int)
                # Clamp to valid range
                x1, y1 = max(x1, 0), max(y1, 0)
                x2, y2 = min(x2, W - 1), min(y2, H - 1)
                bboxes.append((x1, y1, x2, y2))

        # ------------------------------------------------------------------
        # 2. Differentiable crop → ( B , 3 , 112 , 112 )
        # ------------------------------------------------------------------
        face_tensors = []
        for i, bb in enumerate(bboxes):
            if bb is None:
                crop = torch.zeros((1, 3, 112, 112), device=images.device)
            else:
                x1, y1, x2, y2 = bb
                crop = images[i: i + 1, :, y1: y2 + 1, x1: x2 + 1]  # keeps grad
                crop = F.interpolate(crop, size=(112, 112),
                                     mode='bilinear', align_corners=False)
            face_tensors.append(crop)

        faces = torch.cat(face_tensors, dim=0)  # (B, 3, 112, 112)

        # ------------------------------------------------------------------
        # 3. Embeddings
        # ------------------------------------------------------------------
        embeds, _ = self.model(faces)  # (B, D)

        # ------------------------------------------------------------------
        # 4. L2 distance to reference embedding
        # ------------------------------------------------------------------
        if self.side_info is None:
            raise RuntimeError("Call set_side_info(...) first.")
        # distances = torch.norm(embeds - self.side_info, dim=1)  # (B,)
        distances = ((embeds - self.side_info) ** 2).sum(dim=1)

        # ------------------------------------------------------------------
        # 5. Back‑prop to get ∂distance/∂image
        # ------------------------------------------------------------------
        images.grad = None  # clear old grads
        distances.sum().backward()
        grads = images.grad
        if grads is None:
            grads = torch.zeros_like(images)

        return grads.detach()


@register_reward_method('measurement')
class MeasurementReward(Reward):
    def __init__(self, scale=1, **kwargs):
        super().__init__(**kwargs)
        self.operator = None
        self.scale = scale
        self.name = 'measurement'
        self.sigma = None

    def get_reward(self, images: torch.Tensor, **kwargs) -> torch.Tensor:
        dists = - torch.norm(kwargs.get('measurements') - self.operator.measure(images, input_sigma=0), p=2,
                             dim=(1, 2, 3))
        # min_dist = self.sigma * np.sqrt(kwargs.get('measurements')[0].numel())
        # print('', flush=True)
        # print('distances are: ', dists, flush=True)
        # print('min distance is: ', min_dist, flush=True)

        # return -torch.abs(dists - min_dist)
        return dists

    def get_gradients(self, images: torch.Tensor, **kwargs) -> torch.Tensor:
        return self.operator.gradient(images, kwargs.get('measurements'), return_loss=True)

    def set_operator(self, operator):
        self.operator = operator
        self.sigma = self.operator.sigma

    def set_side_info(self, index: int, **kwargs):
        pass

'''
@register_reward_method('text-alignment')
class TextAlignmentReward(Reward):
    """prompt:
    I will give you an image. Your task is to write a single, concise caption for this image.
    The caption must be:
    Less than 70 tokens.
    Accurate and realistic, describing what’s in the original image.
    Optimized to help reconstruct the original image from a degraded version (e.g., blurred, downsampled, or partially masked).
    Aligned with how the CLIP model would interpret the image and caption together.
    ⚠️ Important:
    Make sure you also describe fine-grained object details
    Small or subtle elements
    Spatial relationships that might become ambiguous
    Specific attributes like textures, relative positions, colors, or numbers of similar objects
    Avoid unnecessary words or generic phrases. Use precise and informative language.
    """

    """
    new prompt: 
    You are given an image. Describe it in under 70 tokens, focusing on detailed visual features that help reconstruct the image if parts are masked. Include:

    Object types, number, and relative sizes
    Colors, textures, and materials
    Positions and spatial layout (e.g., left, center, background)
    Orientations and directions (e.g., facing left, upright)
    Lighting, shadows, occlusions
    Symmetry, perspective, or repeated structures
    Be concise, realistic, and aligned with CLIP’s vision-language space. Avoid vague or emotional language.
    """

    """
    You are given an image. Describe it in under 70 tokens, focusing on detailed visual features that help reconstruct the image if parts are masked. Include:

    Object types, number, and relative sizes
    Colors, textures, and materials
    Positions and spatial layout (e.g., left, center, background)
    Orientations and directions (e.g., facing left, upright)
    Lighting, shadows, occlusions
    Symmetry, perspective, or repeated structures
    Focus mainly on the primary object in the image. Mention background or surrounding elements briefly. Be concise, realistic, and aligned with CLIP’s vision-language space. Avoid vague or emotional language.
    """

    def __init__(self, data_path: str = 'dataset/si_daps/additional_texts', pretrained_model: str = 'ViT-B/32',
                 resolution: int = 256, device: str = 'cuda:0', scale=1, **kwargs):
        """
        Initializes the AdaFaceReward class.

        Args:
            data_path (str): Path to the directory containing face images.
            pretrained_model (str): Name of the pretrained AdaFace model to load.
            resolution (int): Target resolution to resize and center crop images.
            device (str): Torch device for inference, default is 'cuda:0'.
            **kwargs: Additional unused keyword arguments.
        """
        super().__init__(**kwargs)
        file_types = ['*.txt']
        self.device = device

        files = [f for f in os.listdir(data_path) if f.lower().endswith('.txt')]
        files.sort()
        self.files: List[str] = [os.path.join(data_path, f) for f in files]
        print(30 * '-')
        print('in constructor of TextAlignmentReward: ')
        print('self.files are: ', self.files)
        print(30 * '-')


        # this will download the ViT-B/32 weights on first run
        self.model, self.preprocess = clip.load("ViT-B/32", device=device)
        self.model.eval()
        self.side_info = None
        self.scale = scale
        self.name = 'text-alignment'

        self.__channel_mean = torch.tensor([0.48145466, 0.4578275, 0.40821073], device=self.device).view(1, 3, 1, 1)
        self.__channel_sd = torch.tensor([0.26862954, 0.26130258, 0.27577711], device=self.device).view(1, 3, 1, 1)


    def get_reward(self, images: torch.Tensor, **kwargs) -> torch.Tensor:
        embed = self._embeddings(images)
        return - torch.norm(self.side_info - embed, dim=1)


    def set_side_info(self, index: int) -> None:

        path = self.files[index]
        with open(path, 'r', encoding='utf-8') as fp:
            text = fp.read()

        tokens = clip.tokenize([text]).to(self.device)  # shape: [1, token_len]

        num_tokens = (tokens != 0).sum().item()  # Count non-padding tokens
        print(f"Number of tokens in the text: {num_tokens}")

        with torch.no_grad():
            text_feats = self.model.encode_text(tokens)  # shape: [1, D]
        self.side_info = text_feats.detach()
        print(30 * '-')
        print('in set_side_info: ')
        print('shape of side_info: ', self.side_info.shape)
        print(30 * '-')

    def _embeddings(self, tensor_images):
        tensor_images = (tensor_images + 1) / 2

        tensor_images = F.interpolate(
            tensor_images.to(self.device),
            size=self.model.visual.input_resolution,
            mode="bilinear",
            align_corners=False
        )
        tensor_images = (tensor_images - self.__channel_mean) / self.__channel_sd

        with torch.no_grad():
            embeddings = self.model.encode_image(tensor_images)  # shape [B, D]

        print(30 * '-', flush=True)
        print('shape of embeddings are: ', embeddings.shape, flush=True)
        print(30 * '-', flush=True)

        return embeddings


    def get_gradients(self, particles, **kwargs) -> torch.Tensor:
        return None
'''


@register_reward_method('text-alignment')
class TextAlignmentReward(Reward):
    def __init__(self, data_path: str = 'dataset/si_daps/additional_texts', pretrained_model: str = 'ImageReward-v1.0',
                 resolution: int = 256, device: str = 'cuda:0', scale=1, **kwargs):
        super().__init__(**kwargs)
        file_types = ['*.txt']
        self.device = device

        files = [f for f in os.listdir(data_path) if f.lower().endswith('.txt')]
        files.sort()
        self.files: List[str] = [os.path.join(data_path, f) for f in files]
        print(30 * '-')
        print('in constructor of TextAlignmentReward: ')
        print('self.files are: ', self.files)
        print(30 * '-')

        # this will download the ViT-B/32 weights on first run
        self.model = RM.load(pretrained_model, device=device)
        #self.model, self.preprocess = clip.load("ViT-B/32", device=device)
        self.model.eval()
        self.side_info = None
        self.scale = scale
        self.name = 'text-alignment'


    def get_reward(self, images: torch.Tensor, **kwargs) -> torch.Tensor:
        tensor_images = ((images + 1) / 2 * 255).clamp(0, 255).byte()

        print(30 * '$', flush=True)
        print(torch.norm(images[0] - images[1]), flush=True)
        print(images.min(), flush=True)
        print(images.max(), flush=True)
        print(30 * '$', flush=True)

        to_pil = transforms.ToPILImage()

        # convert each tensor to a PIL image that ImageReward expects
        pil_imgs = [to_pil(img.cpu()) for img in tensor_images]

        with torch.no_grad():
            _, rewards = self.model.inference_rank(self.side_info, pil_imgs)  # `rewards` is a list of floats

        print(30 * '-', flush=True)
        print('rewards are: ', rewards)
        print(30 * '-', flush=True)
        return torch.tensor(rewards).to(self.device)

    def set_side_info(self, index: int) -> None:
        path = self.files[index]
        with open(path, 'r', encoding='utf-8') as fp:
            text = fp.read()
        self.side_info = text
        print(30 * '-', flush=True)
        print('side info is: ', self.side_info)
        print(30 * '-', flush=True)

    def get_gradients(self, particles, **kwargs) -> torch.Tensor:
        return None