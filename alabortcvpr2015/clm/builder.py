from __future__ import division
from copy import deepcopy

import numpy as np
from scipy.stats import multivariate_normal

from menpo.transform import Scale, Translation, GeneralizedProcrustesAnalysis
from menpo.model import PCAModel
from menpo.shape import mean_pointcloud
from menpo.visualize import print_dynamic, progress_bar_str

from menpofast.utils import build_parts_image

from menpofit.base import build_sampling_grid

from alabortcvpr2015.utils import fsmooth

from .classifier import MCF, MultipleMCF, LinearSVMLR, MultipleLinearSVMLR


class CLMBuilder(object):

    def __init__(self, classifier=MCF, parts_shape=(17, 17),
                 offsets=np.array([[0, 0]]), features=None,
                 normalize_parts=False, covariance=2, diagonal=None,
                 sigma=None, scales=(1, .5), scale_shapes=True,
                 scale_features=True, max_shape_components=None):

        self.classifier = classifier
        self.parts_shape = parts_shape
        self.offsets = offsets
        self.normalize_parts = normalize_parts
        self.covariance = covariance
        self.features = features
        self.diagonal = diagonal
        self.sigma = sigma
        self.scales = list(scales)
        self.scale_shapes = scale_shapes
        self.scale_features = scale_features
        self.max_shape_components = max_shape_components

    def build(self, images, group=None, label=None, verbose=False, **kwargs):
        # compute reference shape
        reference_shape = self._compute_reference_shape(images, group, label,
                                                        verbose)
        # normalize images
        images = self._normalize_images(images, group, label, reference_shape,
                                        verbose)

        # build models at each scale
        if verbose:
            print_dynamic('- Building models\n')
        shape_models = []
        classifiers = []
        # for each pyramid level (high --> low)
        for j, s in enumerate(self.scales):
            if verbose:
                if len(self.scales) > 1:
                    level_str = '  - Level {}: '.format(j)
                else:
                    level_str = '  - '

            # obtain image representation
            if j == 0:
                # compute features at highest level
                feature_images = self._compute_features(images, level_str,
                                                        verbose)
                level_images = feature_images
            elif self.scale_features:
                # scale features at other levels
                level_images = self._scale_images(feature_images, s,
                                                  level_str, verbose)
            else:
                # scale images and compute features at other levels
                scaled_images = self._scale_images(images, s, level_str,
                                                   verbose)
                level_images = self._compute_features(scaled_images,
                                                      level_str, verbose)

            # extract potentially rescaled shapes ath highest level
            level_shapes = [i.landmarks[group][label]
                            for i in level_images]

            # obtain shape representation
            if j == 0 or self.scale_shapes:
                # obtain shape model
                if verbose:
                    print_dynamic('{}Building shape model'.format(level_str))
                shape_model = self._build_shape_model(
                    level_shapes, self.max_shape_components)
                # add shape model to the list
                shape_models.append(shape_model)
            else:
                # copy precious shape model and add it to the list
                shape_models.append(deepcopy(shape_model))

            # obtain parts images
            parts_images = self._parts_images(level_images, level_shapes,
                                              level_str, verbose)

            # build desired responses
            mvn = multivariate_normal(mean=np.zeros(2), cov=self.covariance)
            grid = build_sampling_grid(self.parts_shape)
            Y = [mvn.pdf(grid + offset) for offset in self.offsets]

            # build classifiers
            n_landmarks = level_shapes[0].n_points
            level_classifiers = []
            for l in range(n_landmarks):
                if verbose:
                    print_dynamic('{}Building classifiers - {}'.format(
                        level_str,
                        progress_bar_str((l + 1.) / n_landmarks,
                                         show_bar=False)))

                X = [i.pixels[:, l, ...] for i in parts_images]

                clf = self.classifier(X, Y, **kwargs)
                level_classifiers.append(clf)

            # build Multiple classifier
            if self.classifier is MCF:
                multiple_clf = MultipleMCF(level_classifiers)
            elif self.classifier is LinearSVMLR:
                multiple_clf = MultipleLinearSVMLR(level_classifiers)

            # add appearance model to the list
            classifiers.append(multiple_clf)

            if verbose:
                print_dynamic('{}Done\n'.format(level_str))

        # reverse the list of shape models and classifiers so that they are
        # ordered from lower to higher resolution
        shape_models.reverse()
        classifiers.reverse()
        self.scales.reverse()

        clm = CLM(shape_models, classifiers, reference_shape,
                  self.parts_shape, self.features, self.normalize_parts,
                  self.sigma, self.scales, self.scale_shapes,
                  self.scale_features)

        return clm

    def _compute_reference_shape(self, images, group, label, verbose):
        # the reference_shape is the mean shape of the images' landmarks
        if verbose:
            print_dynamic('- Computing reference shape')
        shapes = [i.landmarks[group][label] for i in images]
        ref_shape = mean_pointcloud(shapes)
        # fix the reference_shape's diagonal length if specified
        if self.diagonal:
            x, y = ref_shape.range()
            scale = self.diagonal / np.sqrt(x**2 + y**2)
            Scale(scale, ref_shape.n_dims).apply_inplace(ref_shape)
        return ref_shape

    def _normalize_images(self, images, group, label, ref_shape, verbose):
        # normalize the scaling of all images wrt the reference_shape size
        norm_images = []
        for c, i in enumerate(images):
            if verbose:
                print_dynamic('- Normalizing images size: {}'.format(
                    progress_bar_str((c + 1.) / len(images), show_bar=False)))
            i = i.rescale_to_reference_shape(ref_shape, group=group,
                                             label=label)
            if self.sigma:
                i.pixels = fsmooth(i.pixels, self.sigma)
            norm_images.append(i)
        return norm_images

    def _compute_features(self, images, level_str, verbose):
        feature_images = []
        for c, i in enumerate(images):
            if verbose:
                print_dynamic(
                    '- Computing feature space: {}'.format(
                        level_str, progress_bar_str((c + 1.) / len(images),
                                                    show_bar=False)))
            if self.features:
                i = self.features(i)
            feature_images.append(i)

        return feature_images

    @classmethod
    def _scale_images(cls, images, s, level_str, verbose):
        scaled_images = []
        for c, i in enumerate(images):
            if verbose:
                print_dynamic(
                    '- Scaling features: {}'.format(
                        level_str, progress_bar_str((c + 1.) / len(images),
                                                    show_bar=False)))
            scaled_images.append(i.rescale(s))
        return scaled_images

    @classmethod
    def _build_shape_model(cls, shapes, max_components):
        r"""
        Builds a shape model given a set of shapes.

        Parameters
        ----------
        shapes: list of :map:`PointCloud`
            The set of shapes from which to build the model.
        max_components: None or int or float
            Specifies the number of components of the trained shape model.
            If int, it specifies the exact number of components to be retained.
            If float, it specifies the percentage of variance to be retained.
            If None, all the available components are kept (100% of variance).

        Returns
        -------
        shape_model: :class:`menpo.model.pca`
            The PCA shape model.
        """

        # centralize shapes
        centered_shapes = [Translation(-s.centre()).apply(s) for s in shapes]
        # align centralized shape using Procrustes Analysis
        gpa = GeneralizedProcrustesAnalysis(centered_shapes)
        aligned_shapes = [s.aligned_source() for s in gpa.transforms]
        # build shape model
        shape_model = PCAModel(aligned_shapes)
        if max_components is not None:
            # trim shape model if required
            shape_model.trim_components(max_components)

        return shape_model

    def _parts_images(self, images, shapes, level_str, verbose):

        # extract parts
        parts_images = []
        for c, (i, s) in enumerate(zip(images, shapes)):
            if verbose:
                print_dynamic('{}Warping images - {}'.format(
                    level_str,
                    progress_bar_str(float(c + 1) / len(images),
                                     show_bar=False)))
            parts_image = build_parts_image(
                i, s, self.parts_shape, offsets=self.offsets,
                normalize_parts=self.normalize_parts)
            parts_images.append(parts_image)

        return parts_images


from .base import CLM
