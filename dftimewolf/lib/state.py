# -*- coding: utf-8 -*-
"""This class maintains the internal dfTimewolf state.

Use it to track errors, abort on global failures, clean up after modules, etc.
"""

from concurrent.futures import ThreadPoolExecutor, Future
import importlib
import logging
import time
import threading
import traceback
from typing import TYPE_CHECKING, Callable, Dict, List, Optional, Sequence, Type, Any, TypeVar, Union  # pylint: disable=line-too-long
from dftimewolf.cli import curses_display_manager as cdm

from dftimewolf.config import Config
from dftimewolf.lib import errors, utils
from dftimewolf.lib import telemetry
from dftimewolf.lib.containers import interface
from dftimewolf.lib.containers import manager as container_manager
from dftimewolf.lib.containers.interface import AttributeContainer
from dftimewolf.lib.errors import DFTimewolfError
from dftimewolf.lib.modules import manager as modules_manager
from dftimewolf.lib.module import ThreadAwareModule, BaseModule

if TYPE_CHECKING:
  from dftimewolf.lib import module as dftw_module

T = TypeVar("T", bound="interface.AttributeContainer")  # pylint: disable=invalid-name,line-too-long

logger = logging.getLogger('dftimewolf.state')

NEW_ISSUE_URL = 'https://github.com/log2timeline/dftimewolf/issues/new'

TELEMETRY = telemetry


class DFTimewolfState(object):
  """The main State class.

  Attributes:
    command_line_options (dict[str, Any]): Command line options passed to
        dftimewolf.
    config (dftimewolf.config.Config): Class to be used throughout execution.
    errors (list[tuple[str, bool]]): errors generated by a module. These
        should be cleaned up after each module run using the CleanUp() method.
    global_errors (list[tuple[str, bool]]): the CleanUp() method moves non
        critical errors to this attribute for later reporting.
    input (list[str]): data that the current module will use as input.
    output (list[str]): data that the current module generates.
    recipe: (dict[str, str]): recipe declaring modules to load.
    store (dict[str, object]): arbitrary data for modules.
    telemetry_store: store for statistics generated by modules.
  """

  def __init__(self, config: Type[Config]) -> None:
    """Initializes a state."""
    super(DFTimewolfState, self).__init__()
    self.command_line_options = {}  # type: Dict[str, Any]
    self._cache = {}  # type: Dict[str, str]
    self._module_pool = {}  # type: Dict[str, BaseModule]
    self._state_lock = threading.Lock()
    self._threading_event_per_module = {}  # type: Dict[str, threading.Event]
    self.config = config
    self.errors = []  # type: List[DFTimewolfError]
    self.global_errors = []  # type: List[DFTimewolfError]
    self.recipe = {}  # type: Dict[str, Any]
    self._container_manager = container_manager.ContainerManager(logger)
    self.streaming_callbacks = {}  # type: Dict[Type[interface.AttributeContainer], List[Callable[[Any], Any]]]  # pylint: disable=line-too-long
    self._abort_execution = False
    self.stdout_log = True
    self._progress_warning_shown = False
    self.telemetry: Union[telemetry.BaseTelemetry, telemetry.GoogleCloudSpannerTelemetry, None] = None # pylint: disable=line-too-long

  def _InvokeModulesInThreads(self, callback: Callable[[Any], Any]) -> None:
    """Invokes the callback function on all the modules in separate threads.

    Args:
      callback (function): callback function to invoke on all the modules.
    """
    threads = []
    for module_definition in self.recipe['modules']:
      thread_args = (module_definition,)
      thread = threading.Thread(target=callback, args=thread_args)
      threads.append(thread)
      thread.start()

    for thread in threads:
      thread.join()

    self.CheckErrors(is_global=True)

  def ImportRecipeModules(self, module_locations: Dict[str, str]) -> None:
    """Dynamically loads the modules declared in a recipe.

    Args:
      module_locations: A dfTimewolf module name - Python module
          mapping. e.g.:
            {'GRRArtifactCollector': 'dftimewolf.lib.collectors.grr_hosts'}

    Raises:
      errors.RecipeParseError: if a module requested in a recipe does not
          exist in the mapping.
    """
    for module in self.recipe['modules'] + self.recipe.get('preflights', []):
      name = module['name']
      if name.endswith('Legacy'):
        msg = f'Skipping legacy module {name}, rename module to use.'
        logger.warning(msg)
        continue
      if name not in module_locations:
        msg = (
            f'In {self.recipe["name"]}: module {name} cannot be found. '
            'It may not have been declared.')
        raise errors.RecipeParseError(msg)
      logger.debug(
          'Loading module {0:s} from {1:s}'.format(
              name, module_locations[name]))

      location = module_locations[name]
      try:
        importlib.import_module(location)
      except ModuleNotFoundError as exception:
        msg = f'Cannot find Python module for {name} ({location}): {exception}'
        raise errors.RecipeParseError(msg)

  def LoadRecipe(
      self, recipe: Dict[str, Any], module_locations: Dict[str, str]) -> None:
    """Populates the internal module pool with modules declared in a recipe.

    Args:
      recipe (dict[str, Any]): recipe declaring modules to load.

    Raises:
      RecipeParseError: if a module in the recipe has not been declared.
    """
    self.recipe = recipe
    module_definitions = recipe.get('modules', [])
    preflight_definitions = recipe.get('preflights', [])
    self.ImportRecipeModules(module_locations)
    self._container_manager.ParseRecipe(recipe)

    for module_definition in module_definitions + preflight_definitions:
      # Combine CLI args with args from the recipe description
      module_name = module_definition['name']
      runtime_name = module_definition.get('runtime_name')
      if not runtime_name:
        runtime_name = module_name
      module_class = modules_manager.ModulesManager.GetModuleByName(module_name)
      if module_class:
        # pytype: disable=wrong-arg-types
        self._module_pool[runtime_name] = module_class(self, name=runtime_name)
        # pytype: enable=wrong-arg-types

  def FormatExecutionPlan(self) -> str:
    """Formats execution plan.

    Returns information about loaded modules and their corresponding arguments
    to stdout.

    Returns:
      str: String representation of loaded modules and their parameters.
    """
    plan = ""
    maxlen = 0

    modules = self.recipe.get('preflights', []) + self.recipe.get('modules', [])

    for module in modules:
      if not module['args']:
        continue
      spacing = len(max(module['args'].keys(), key=len))
      maxlen = maxlen if maxlen > spacing else spacing

    for module in modules:
      runtime_name = module.get('runtime_name')
      if runtime_name:
        plan += '{0:s} ({1:s}):\n'.format(runtime_name, module['name'])
      else:
        plan += '{0:s}:\n'.format(module['name'])

      if not module['args']:
        plan += '  *No params*\n'
      for key, value in module['args'].items():
        plan += '  {0:s}{1:s}\n'.format(key.ljust(maxlen + 3), repr(value))

    return plan

  def LogExecutionPlan(self) -> None:
    """Logs the result of FormatExecutionPlan() using the base logger."""
    for line in self.FormatExecutionPlan().split('\n'):
      logger.debug(line)

  def AddToCache(self, name: str, value: Any) -> None:
    """Thread-safe method to add data to the state's cache.

    If the cached item is already in the cache it will be
    overwritten with the new value.

    Args:
      name (str): string with the name of the cache variable.
      value (object): the value that will be stored in the cache.
    """
    with self._state_lock:
      self._cache[name] = value

  def GetFromCache(self, name: str, default_value: Any = None) -> Any:
    """Thread-safe method to get data from the state's cache.

    Args:
      name (str): string with the name of the cache variable.
      default_value (object): the value that will be returned if
          the item does not exist in the cache. Optional argument
          and defaults to None.

    Returns:
      object: object from the cache that corresponds to the name, or
          the value of "default_value" if the cache does not contain
          the variable.
    """
    with self._state_lock:
      return self._cache.get(name, default_value)

  def StoreContainer(
      self,
      container: "interface.AttributeContainer",
      source_module: str) -> None:
    """Thread-safe method to store data in the state's store.

    Args:
      container: data to store.
      source_module: the originating module.
    """
    self._container_manager.StoreContainer(source_module=source_module,
                                           container=container)

  def LogTelemetry(
      self, telemetry_entry: telemetry.TelemetryCollection) -> None:
    """Method to store telemetry in the state's telemetry store.

    Args:
      telemetry_entry: The telemetry object to store.
    """
    for key, value in telemetry_entry.telemetry.items():
      if self.telemetry is not None:
        self.telemetry.LogTelemetry(
            key, value, telemetry_entry.module_name, telemetry_entry.recipe)

  def GetContainers(
      self,
      requesting_module: str,
      container_class: Type[T],
      pop: bool = False,
      metadata_filter_key: Optional[str] = None,
      metadata_filter_value: Optional[Any] = None) -> Sequence[T]:
    """Retrieve previously stored containers.

    Args:
      requesting_module: The name of the module making the retrieval.
      container_class (type): AttributeContainer class used to filter data.
      pop (Optional[bool]): Whether to remove the containers from the state when
          they are retrieved.
      metadata_filter_key (Optional[str]): Metadata key to filter on.
      metadata_filter_value (Optional[Any]): Metadata value to filter on.

    Returns:
      Collection[AttributeContainer]: attribute container objects provided in
          the store that correspond to the container type.

    Raises:
      RuntimeError: If only one metadata filter parameter is specified.
    """
    return self._container_manager.GetContainers(
        requesting_module=requesting_module,
        container_class=container_class,
        pop=pop,
        metadata_filter_key=metadata_filter_key,
        metadata_filter_value=metadata_filter_value)

  def _SetupModuleThread(self, module_definition: Dict[str, str]) -> None:
    """Calls the module's SetUp() function and sets a threading event for it.

    Callback for _InvokeModulesInThreads.

    Args:
      module_definition (dict[str, str]): recipe module definition.
    """
    module_name = module_definition['name']
    runtime_name = module_definition.get('runtime_name', module_name)
    logger.info('Setting up module: {0:s}'.format(runtime_name))
    new_args = utils.ImportArgsFromDict(
        module_definition['args'], self.command_line_options, self.config)
    module = self._module_pool[runtime_name]

    try:
      self._RunModuleSetUp(module, **new_args)
    except errors.DFTimewolfError:
      msg = "A critical error occurred in module {0:s}, aborting execution."
      logger.critical(msg.format(module.name))
    except Exception as exception:  # pylint: disable=broad-except
      msg = 'An unknown error occurred in module {0:s}: {1!s}'.format(
          module.name, exception)
      logger.critical(msg)
      # We're catching any exception that is not a DFTimewolfError, so we want
      # to generate an error for further reporting.
      error = errors.DFTimewolfError(
          message=msg,
          name='dftimewolf',
          stacktrace=traceback.format_exc(),
          critical=True,
          unexpected=True)
      self.AddError(error)

    self._threading_event_per_module[runtime_name] = threading.Event()
    self.CleanUp()

  def _RunModuleSetUp(
      self, module: BaseModule, **new_args: Dict[str, object]) -> None:
    """Runs SetUp of a single module.

    Designed to be wrapped by an output handling subclass.

    Args:
      module: The modulke that will have SetUp called.
      new_args: kwargs to pass to SetUp."""
    module.SetUp(**new_args)

  def _RunModuleProcess(self, module: BaseModule) -> None:
    """Runs Process of a single module.

    Designed to be wrapped by an output handling subclass.

    Args:
      module: The module to run Process() on."""
    time_start = time.time()
    module.Process()
    total_time = utils.CalculateRunTime(time_start)
    module.LogTelemetry({'total_time': str(total_time)})

  def _RunModuleProcessThreaded(
      self, module: ThreadAwareModule) -> List[Future]:  # type: ignore
    """Runs Process of a single ThreadAwareModule module.

    Designed to be wrapped by an output handling subclass.

    Args:
      module: The module that will have Process(container) called in a threaded
          fashion.

    Returns:
      List of futures for the threads that were started.
    """
    containers = self.GetContainers(
        requesting_module=module.name,
        container_class=module.GetThreadOnContainerType(),
        pop=not module.KeepThreadedContainersInState())
    logger.info(
        f'Running {len(containers)} threads, max {module.GetThreadPoolSize()} '
        f'simultaneous for module {module.name}')

    futures = []

    with ThreadPoolExecutor(max_workers=module.GetThreadPoolSize()) \
        as executor:
      for c in containers:
        logger.debug(f'Launching {module.name}.Process thread with {str(c)}')
        time_start = time.time()
        futures.append(executor.submit(module.Process, c))
        total_time = utils.CalculateRunTime(time_start)
        module.LogTelemetry({'total_time': str(total_time)})
    return futures

  def _RunModulePreProcess(self, module: ThreadAwareModule) -> None:
    """Runs PreProcess of a single module.

    Designed to be wrapped by an output handling subclass.

    Args:
      module: The module that will have PreProcess() called."""
    module.PreProcess()

  def _RunModulePostProcess(self, module: ThreadAwareModule) -> None:
    """Runs PostProcess of a single module.

    Designed to be wrapped by an output handling subclass.

    Args:
      module: The module that will have PostProcess() called."""
    module.PostProcess()

  # pylint: disable=unused-argument
  def _HandleFuturesFromThreadedModule(
      self,
      futures: List[Future],  # type: ignore
      runtime_name: str) -> None:
    """Handles any futures raised by the async processing of a module.

    Args:
      futures: A list of futures, returned by RunModuleProcessThreaded().
      runtime_name: runtime name of the module."""
    for fut in futures:
      if fut.exception():
        raise fut.exception()  # type: ignore

  # pylint: disable=unused-argument

  def SetupModules(self) -> None:
    """Performs setup tasks for each module in the module pool.

    Threads declared modules' SetUp() functions. Takes CLI arguments into
    account when replacing recipe parameters for each module.
    """
    # Note that vars() copies the values of argparse.Namespace to a dict.
    self._InvokeModulesInThreads(self._SetupModuleThread)

  def _RunModuleThread(self, module_definition: Dict[str, str]) -> None:
    """Runs the module's Process() function.

    Callback for _InvokeModulesInThreads.

    Waits for any blockers to have finished before running Process(), then
    sets an Event flag declaring the module has completed.

    Args:
      module_definition (dict): module definition.
    """
    module_name = module_definition['name']
    runtime_name = module_definition.get('runtime_name', module_name)

    for dependency in module_definition['wants']:
      self._threading_event_per_module[dependency].wait()

    module = self._module_pool[runtime_name]

    # Abort processing if a module has had critical failures before.
    if self._abort_execution:
      logger.critical(
          'Aborting execution of {0:s} due to previous errors'.format(
              module.name))
      self._threading_event_per_module[runtime_name].set()
      self.CleanUp()
      return

    logger.info('Running module: {0:s}'.format(runtime_name))

    try:
      if isinstance(module, ThreadAwareModule):
        self._RunModulePreProcess(module)
        futures = self._RunModuleProcessThreaded(module)
        self._RunModulePostProcess(module)
        self._HandleFuturesFromThreadedModule(futures, runtime_name)
      else:
        self._RunModuleProcess(module)
    except errors.DFTimewolfError:
      logger.critical(
          "Critical error in module {0:s}, aborting execution".format(
              module.name))
    except Exception as exception:  # pylint: disable=broad-except
      msg = 'An unknown error occurred in module {0:s}: {1!s}'.format(
          module.name, exception)
      logger.critical(msg)
      # We're catching any exception that is not a DFTimewolfError, so we want
      # to generate an error for further reporting.
      error = errors.DFTimewolfError(
          message=msg,
          name='dftimewolf',
          stacktrace=traceback.format_exc(),
          critical=True,
          unexpected=True)
      self.AddError(error)

    logger.info('Module {0:s} finished execution'.format(runtime_name))
    self._threading_event_per_module[runtime_name].set()

    try:
      self._container_manager.CompleteModule(runtime_name)
    except Exception:  # pylint: disable=broad-exception-caught
      logger.warning('Unknown exception encountered', exc_info=True)

    self.CleanUp()

  def RunPreflights(self) -> None:
    """Runs preflight modules."""
    for preflight_definition in self.recipe.get('preflights', []):
      preflight_name = preflight_definition['name']
      runtime_name = preflight_definition.get('runtime_name', preflight_name)

      args = preflight_definition.get('args', {})

      new_args = utils.ImportArgsFromDict(
          args, self.command_line_options, self.config)
      preflight = self._module_pool[runtime_name]
      try:
        self._RunModuleSetUp(preflight, **new_args)
        self._RunModuleProcess(preflight)
        self._threading_event_per_module[runtime_name] = threading.Event()
        self._threading_event_per_module[runtime_name].set()
      finally:
        self.CheckErrors(is_global=True)

  def CleanUpPreflights(self) -> None:
    """Executes any cleanup actions defined in preflight modules."""
    for preflight_definition in self.recipe.get('preflights', []):
      preflight_name = preflight_definition['name']
      runtime_name = preflight_definition.get('runtime_name', preflight_name)
      preflight = self._module_pool[runtime_name]
      try:
        preflight.CleanUp()
      finally:
        self.CheckErrors(is_global=True)

  def InstantiateModule(self, module_name: str) -> Optional["BaseModule"]:
    """Instantiates an arbitrary dfTimewolf module.

    Args:
      module_name (str): The name of the module to instantiate.

    Returns:
      BaseModule: An instance of a dftimewolf Module, which is a subclass of
          BaseModule, or None if the module could not be found.
    """
    module_class: Optional[Type["BaseModule"]]
    module_class = modules_manager.ModulesManager.GetModuleByName(module_name)
    # pytype: disable=wrong-arg-types
    if module_class:
      return module_class(self)
    # pytype: enable=wrong-arg-types
    return None

  def RunModules(self) -> None:
    """Performs the actual processing for each module in the module pool."""
    self._InvokeModulesInThreads(self._RunModuleThread)

  def RegisterStreamingCallback(
      self, target: Callable[[T], Any], container_type: Type[T]) -> None:
    """Registers a callback for a type of container.

    The function to be registered should a single parameter of type
    interface.AttributeContainer.

    Args:
      target (function): function to be called.
      container_type (type[interface.AttributeContainer]): container type on
          which the callback will be called.
    """
    if container_type not in self.streaming_callbacks:
      self.streaming_callbacks[container_type] = []
    self.streaming_callbacks[container_type].append(target)

  def StreamContainer(
      self,
      container: "interface.AttributeContainer",
      source_module: str = "") -> None:
    """Streams a container to the callbacks that are registered to handle it.

    Args:
      container: container instance that will be streamed to any
          registered callbacks.
      source_module: the originating module.
    """
    for callback in self.streaming_callbacks.get(type(container), []):
      callback(container)

  def AddError(self, error: DFTimewolfError) -> None:
    """Adds an error to the state.

    Args:
      error (errors.DFTimewolfError): The dfTimewolf error to add.
    """
    if error.critical:
      self._abort_execution = True
    self.errors.append(error)

  def CleanUp(self) -> None:
    """Cleans up after running a module.

    The state's output becomes the input for the next stage. Any errors are
    moved to the global_errors attribute so that they can be reported at a
    later stage.
    """
    # Move any existing errors to global errors
    self.global_errors.extend(self.errors)
    self.errors = []

  def CheckErrors(self, is_global: bool = False) -> None:
    """Checks for errors and exits if any of them are critical.

    Args:
      is_global (Optional[bool]): True if the global_errors attribute should
          be checked. False if the error attribute should be checked.

    Raises:
      errors.CriticalError: If any critical errors were found.
    """
    error_objects = self.global_errors if is_global else self.errors
    critical_errors = False

    if error_objects:
      logger.error('dfTimewolf encountered one or more errors:')

    for index, error in enumerate(error_objects):
      logger.error(
          '{0:d}: error from {1:s}: {2:s}'.format(
              index + 1, error.name, error.message))
      if error.stacktrace:
        for line in error.stacktrace.split('\n'):
          logger.error(line)
      if error.critical:
        critical_errors = True

    if any(error.unexpected for error in error_objects):
      logger.critical('One or more unexpected errors occurred.')
      logger.critical(
          'Please consider opening an issue: {0:s}'.format(NEW_ISSUE_URL))

    if critical_errors:
      raise errors.CriticalError('Critical error found. Aborting.')

  def PublishMessage(
      self, source: str, message: str, is_error: bool = False) -> None:
    """Receives a message for publishing.

    The base class does nothing with this (as the method in module also logs the
    message). This method exists to be overridden for other UIs.

    Args:
      source: The source of the message.
      message: The message content.
      is_error: True if the message is an error message, False otherwise.
    """

  def ProgressUpdate(
      self, module_name: str, steps_taken: int, steps_expected: int) -> None:
    """Currently unsupported when no UI is in use."""
    if not self._progress_warning_shown:
      self._progress_warning_shown = True
      logger.debug('ProgressUpdate called in unsupported display mode.')

  def ThreadProgressUpdate(
      self, module_name: str, thread_id: str, steps_taken: int,
      steps_expected: int) -> None:
    """Currently unsupported when no UI is in use."""
    if not self._progress_warning_shown:
      self._progress_warning_shown = True
      logger.debug('ProgressUpdate called in unsupported display mode.')


class DFTimewolfStateWithCDM(DFTimewolfState):
  """The main state class, extended to wrap methods with updates to a
  CursesDisplayManager object."""

  def __init__(
      self, config: Type[Config], cursesdm: cdm.CursesDisplayManager) -> None:
    """Initializes a state."""
    super(DFTimewolfStateWithCDM, self).__init__(config)
    self.cursesdm = cursesdm
    self.stdout_log = False

  def LoadRecipe(
      self, recipe: Dict[str, Any], module_locations: Dict[str, str]) -> None:
    """Populates the internal module pool with modules declared in a recipe.

    Args:
      recipe (dict[str, Any]): recipe declaring modules to load.

    Raises:
      RecipeParseError: if a module in the recipe has not been declared.
    """
    super(DFTimewolfStateWithCDM, self).LoadRecipe(recipe, module_locations)

    module_definitions = recipe.get('modules', [])
    preflight_definitions = recipe.get('preflights', [])

    self.cursesdm.SetRecipe(self.recipe['name'])
    for module_definition in preflight_definitions:
      self.cursesdm.EnqueuePreflight(
          module_definition['name'], module_definition.get('wants', []),
          module_definition.get('runtime_name'))
    for module_definition in module_definitions:
      self.cursesdm.EnqueueModule(
          module_definition['name'], module_definition.get('wants', []),
          module_definition.get('runtime_name'))
    self.cursesdm.Draw()

  def _RunModuleSetUp(
      self, module: BaseModule, **new_args: Dict[str, object]) -> None:
    """Runs SetUp of a single module.

    Args:
      module: The modulke that will have SetUp called.
      new_args: kwargs to pass to SetUp."""
    self.cursesdm.UpdateModuleStatus(module.name, cdm.Status.SETTINGUP)
    module.SetUp(**new_args)
    self.cursesdm.UpdateModuleStatus(module.name, cdm.Status.PENDING)

  def _RunModuleProcess(self, module: BaseModule) -> None:
    """Runs Process of a single module.

    Args:
      module: The module to run Process() on."""
    self.cursesdm.UpdateModuleStatus(module.name, cdm.Status.PROCESSING)
    module.Process()
    self.cursesdm.UpdateModuleStatus(module.name, cdm.Status.COMPLETED)

  def _RunModuleProcessThreaded(
      self, module: ThreadAwareModule) -> List[Future]:  # type: ignore
    """Runs Process of a single ThreadAwareModule module.

    Args:
      module: The module that will have Process(container) called in a threaded
          fashion.

    Returns:
      List of futures for the threads that were started.
    """
    containers = self.GetContainers(
        requesting_module=module.name,
        container_class=module.GetThreadOnContainerType(),
        pop=not module.KeepThreadedContainersInState())
    logger.info(
        f'Running {len(containers)} threads, max {module.GetThreadPoolSize()} '
        f'simultaneous for module {module.name}')

    self.cursesdm.SetThreadedModuleContainerCount(module.name, len(containers))
    self.cursesdm.UpdateModuleStatus(module.name, cdm.Status.PROCESSING)

    futures = []

    with ThreadPoolExecutor(max_workers=module.GetThreadPoolSize()) \
        as executor:
      for c in containers:
        futures.append(
            executor.submit(self._WrapThreads, module.Process, c, module.name))

    return futures

  def _RunModulePreProcess(self, module: ThreadAwareModule) -> None:
    """Runs PreProcess of a single module.

    Args:
      module: The module that will have PreProcess() called."""
    self.cursesdm.UpdateModuleStatus(module.name, cdm.Status.PREPROCESSING)
    module.PreProcess()
    self.cursesdm.UpdateModuleStatus(module.name, cdm.Status.PENDING)

  def _RunModulePostProcess(self, module: ThreadAwareModule) -> None:
    """Runs PostProcess of a single module.

    Args:
      module: The module that will have PostProcess() called."""
    self.cursesdm.UpdateModuleStatus(module.name, cdm.Status.POSTPROCESSING)
    module.PostProcess()
    self.cursesdm.UpdateModuleStatus(module.name, cdm.Status.COMPLETED)

  def _HandleFuturesFromThreadedModule(
      self,
      futures: List[Future],  # type: ignore
      runtime_name: str) -> None:
    """Handles any futures raised by the async processing of a module.

    Args:
      futures: A list of futures, returned by RunModuleProcessThreaded().
      runtime_name: runtime name of the module."""
    for fut in futures:
      if fut.exception():
        self.cursesdm.SetError(runtime_name, str(fut.exception()))
        raise fut.exception()  # type: ignore

  def _WrapThreads(
      self, process: Callable[[AttributeContainer], None],
      container: AttributeContainer, module_name: str) -> None:
    """Wraps a ThreadPoolExecutor call to module.process with the
    CursesDisplayManager status update methods.

    Args:
      process: A callable method: Process, belonging to a ThreadAwareModule.
      container: The Container being processed by the thread.
      module_name: The runtime name of the module."""

    thread_id = threading.current_thread().name
    self.cursesdm.UpdateModuleThreadState(
        module_name, cdm.Status.RUNNING, thread_id, str(container))

    process(container)

    self.cursesdm.UpdateModuleThreadState(
        module_name, cdm.Status.COMPLETED, thread_id, str(container))

  def AddError(self, error: DFTimewolfError) -> None:
    """Adds an error to the state.

    Args:
      error (errors.DFTimewolfError): The dfTimewolf error to add.
    """
    super(DFTimewolfStateWithCDM, self).AddError(error)

    name = error.name if error.name else 'no_module_name'
    self.cursesdm.SetError(name, error.message)

  def PublishMessage(
      self, source: str, message: str, is_error: bool = False) -> None:
    """Receives a message for publishing to the list of messages.

    Args:
      source: The source of the message.
      message: The message content.
      is_error: True if the message is an error message, False otherwise."""
    self.cursesdm.EnqueueMessage(source, message, is_error)

  def ProgressUpdate(
      self, module_name: str, steps_taken: int, steps_expected: int) -> None:
    """Set the current completion status of a module.

    Args:
      module_name: The module in question.
      steps_taken: The number of steps taken so far.
      steps_expected: The number of total steps expected for completion.
    """
    self.cursesdm.SetModuleProgress(module_name, steps_taken, steps_expected)

  def ThreadProgressUpdate(
      self, module_name: str, thread_id: str, steps_taken: int,
      steps_expected: int) -> None:
    """Set the current completion status of a module thread.

    Args:
      module_name: The module in question.
      thread_id: The thread id in question.
      steps_taken: The number of steps taken so far.
      steps_expected: The number of total steps expected for completion.
    """
    self.cursesdm.SetModuleThreadProgress(
        module_name, thread_id, steps_taken, steps_expected)
