"""RCVAE - Regularized Conditional Variational Autoencoders"""

from . import models as archs
from . import utils as tl
from . import data_loader as dl
from . import plotting as pl


__author__ = ', '.join([
    'Mohsen Naghipourfar',
    'Mohammad Lotfollahi'
])

__email__ = ', '.join([
    'mohsen.naghipourfar@gmail.de',
    'Mohammad.lotfollahi@helmholtz-muenchen.de',
])

from get_version import get_version
__version__ = get_version(__file__)

del get_version



