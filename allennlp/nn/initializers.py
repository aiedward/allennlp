import logging
import re
from typing import Callable, List, Tuple, Type
import itertools

import torch
from torch.autograd import Variable
import torch.nn.init

from allennlp.common import Registrable
from allennlp.common.params import Params
from allennlp.common.checks import ConfigurationError
logger = logging.getLogger(__name__)  # pylint: disable=invalid-name


class Initializer(Registrable):
    """
    An initializer is really just a bare pytorch function. This class
    is a proxy that allows us to implement ``Registerable`` for those functions.
    """
    default_implementation = 'normal'

    def __call__(self, tensor: torch.autograd.Variable) -> None:
        """
        This function is here just to make mypy happy.  We expect initialization functions to
        follow this API; the builtin pytorch initialization functions follow this just fine, even
        though they don't subclass ``Initialization``.  We're just making it explicit here, so mypy
        knows that initializers are callable like this.
        """
        raise NotImplementedError

    @classmethod
    def from_params(cls, params: Params):
        # Just a string - corresponds to the name of an initializer.
        if isinstance(params, str):
            return cls.by_name(params)()
        else:
            choice = params.pop_choice("type", cls.list_available())
            return cls.by_name(choice).from_params(params)


def block_orthogonal(tensor: torch.Tensor,
                     split_sizes: List[int],
                     gain: float = 1.0) -> None:
    """
    An initializer which allows initializing model parameters in "blocks". This is helpful
    in the case of recurrent models which use multiple gates applied to linear projections,
    which can be computed efficiently if they are concatenated together. However, they are
    separate parameters which should be initialized independently.

    Parameters
    ----------
    tensor : ``torch.Tensor``, required.
        A tensor to initialize.
    split_sizes : List[int], required.
        A list of length ``tensor.ndim()`` specifying the size of the
        blocks along that particular dimension. E.g. ``[10, 20]`` would
        result in the tensor being split into chunks of size 10 along the
        first dimension and 20 along the second.
    gain : float, optional (default = 1.0)
        The gain (scaling) applied to the orthogonal initialization.
    """

    if isinstance(tensor, Variable):
        block_orthogonal(tensor.data, split_sizes, gain)
        return tensor

    sizes = list(tensor.size())
    if any([a % b != 0 for a, b in zip(sizes, split_sizes)]):
        raise ConfigurationError("tensor dimensions must be divisible by their respective "
                                 "split_sizes. Found size: {} and split_sizes: {}".format(sizes, split_sizes))
    indexes = [list(range(0, max_size, split))
               for max_size, split in zip(sizes, split_sizes)]
    # Iterate over all possible blocks within the tensor.
    for block_start_indices in itertools.product(*indexes):
        # A list of tuples containing the index to start at for this block
        # and the appropriate step size (i.e split_size[i] for dimension i).
        index_and_step_tuples = zip(block_start_indices, split_sizes)
        # This is a tuple of slices corresponding to:
        # tensor[index: index + step_size, ...]. This is
        # required because we could have an arbitrary number
        # of dimensions. The actual slices we need are the
        # start_index: start_index + step for each dimension in the tensor.
        block_slice = tuple([slice(start_index, start_index + step)
                             for start_index, step in index_and_step_tuples])
        tensor[block_slice] = torch.nn.init.orthogonal(tensor[block_slice].contiguous(), gain=gain)


def _initializer_wrapper(init_function: Callable[..., None]) -> Type[Initializer]:
    class Init(Initializer):
        def __init__(self, **kwargs):
            self._init_function = init_function
            self._kwargs = kwargs
        def __call__(self, tensor: torch.autograd.Variable) -> None:
            self._init_function(tensor, **self._kwargs)
        def __repr__(self):
            return 'Init: %s, with params: %s' % (self._init_function, self._kwargs)
        @classmethod
        def from_params(cls, params: Params):
            return cls(**params.as_dict())
    return Init


# There are no classes to decorate, so we hack these into Registrable._registry
Registrable._registry[Initializer] = {  # pylint: disable=protected-access
        "normal": _initializer_wrapper(torch.nn.init.normal),
        "uniform": _initializer_wrapper(torch.nn.init.uniform),
        "orthogonal": _initializer_wrapper(torch.nn.init.orthogonal),
        "constant": _initializer_wrapper(torch.nn.init.constant),
        "dirac": _initializer_wrapper(torch.nn.init.dirac),
        "xavier_normal": _initializer_wrapper(torch.nn.init.xavier_normal),
        "xavier_uniform": _initializer_wrapper(torch.nn.init.xavier_uniform),
        "kaiming_normal": _initializer_wrapper(torch.nn.init.kaiming_normal),
        "kaiming_uniform": _initializer_wrapper(torch.nn.init.kaiming_uniform),
        "sparse": _initializer_wrapper(torch.nn.init.sparse),
        "eye": _initializer_wrapper(torch.nn.init.eye),
        "block_orthogonal": _initializer_wrapper(block_orthogonal)
}


class InitializerApplicator:
    """
    Applies initializers to the parameters of a Module based on regex matches.  Any parameter not
    explicitly matching a regex will not be initialized, instead using whatever the default
    initialization was in the module's code.
    """
    def __init__(self, initializers: List[Tuple[str, Initializer]] = None) -> None:
        """
        Parameters
        ----------
        initializers : ``List[Tuple[str, Initializer]]``, optional (default = [])
            A list mapping parameter regexes to initializers.  We will check each parameter against
            each regex in turn, and apply the initializer paired with the first matching regex, if
            any.
        """
        self._initializers = initializers or []

    def __call__(self, module: torch.nn.Module) -> None:
        """
        Applies an initializer to all parameters in a module that match one of the regexes we were
        given in this object's constructor.  Does nothing to parameters that do not match.

        Parameters
        ----------
        module : torch.nn.Module, required.
            The Pytorch module to apply the initializers to.
        """
        logger.info("Initializing parameters")
        unused_regexes = set([initializer[0] for initializer in self._initializers])
        uninitialized_parameters = set()
        # Store which initialisers were applied to which parameters.
        for name, parameter in module.named_parameters():
            for initializer_regex, initializer in self._initializers:
                if re.search(initializer_regex, name):
                    logger.info("Initializing %s using %s intitializer", name, initializer_regex)
                    initializer(parameter)
                    unused_regexes.discard(initializer_regex)
                    break
            else:  # no break
                uninitialized_parameters.add(name)
        for regex in unused_regexes:
            logger.warning("Did not use initialization regex that was passed: %s", regex)
        logger.info("Done initializing parameters; the following parameters are using their "
                    "default initialization from their code")
        for name in uninitialized_parameters:
            logger.info("   %s", name)

    @classmethod
    def from_params(cls, params: List[Tuple[str, Params]]) -> "InitializerApplicator":
        """
        Converts a Params object into an InitializerApplicator. The json should
        be formatted as follows::

            [
                ["parameter_regex_match1",
                    {
                        "type": "normal"
                        "mean": 0.01
                        "std": 0.1
                    }
                ],
                ["parameter_regex_match2", "uniform"]
            ]

        where the first item in each tuple is the regex that matches to parameters, and the second
        item is a set of parameters that will be passed to ``Initialzer.from_params()``.  These
        values can either be strings, in which case they correspond to the names of initializers,
        or dictionaries, in which case they must contain the "type" key, corresponding to the name
        of an initializer.  In addition, they may contain auxiliary named parameters which will be
        fed to the initializer itself. To determine valid auxiliary parameters, please refer to the
        torch.nn.init documentation.

        Returns
        -------
        An InitializerApplicator containing the specified initializers.
        """
        initializers = [(name, Initializer.from_params(init_params)) for name, init_params in params]
        return InitializerApplicator(initializers)
