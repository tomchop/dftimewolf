# -*- coding: utf-8 -*-
"""Export processing results to Timesketch.
Threaded version of existing Timesketch module."""

import time
import uuid
from typing import Optional, List, Type, Union, Set

from timesketch_import_client import importer
from timesketch_api_client import sketch as ts_sketch
from timesketch_api_client import client as ts_client  # pylint: disable=unused-import,line-too-long  # used for typing
from timesketch_api_client import error as ts_error
from timesketch_api_client import analyzer as ts_analyzer

from dftimewolf.lib import module, timesketch_utils
from dftimewolf.lib.containers import containers, interface
from dftimewolf.lib.modules import manager as modules_manager
from dftimewolf.lib.state import DFTimewolfState


class TimesketchExporter(module.ThreadAwareModule):
  """Exports a given set of plaso or CSV files to Timesketch. This is a
  threaded version of an equivalent module.

  input: A list of paths to plaso or CSV files.
  output: A URL to the generated timeline.

  Attributes:
    incident_id (str): Incident ID or reference. Used in sketch description.
    sketch_id (int): Sketch ID to add the resulting timeline to. If not
        provided, a new sketch is created.
    timesketch_api (TimesketchApiClient): Timesketch API client.
  """

  sketch: ts_sketch.Sketch

  def __init__(
      self,
      state: DFTimewolfState,
      name: Optional[str] = None,
      critical: bool = False) -> None:
    super(TimesketchExporter, self).__init__(
        state, name=name, critical=critical)
    self.incident_id = None  # type: Union[str, None]
    self.sketch_id = 0  # type: int
    self.timesketch_api = None  # type: ts_client.TimesketchApi
    self._analyzers = []  # type: List[str]
    self.wait_for_timelines = False  # type: bool
    self.host_url = None  # type: Union[str, None]
    self.sketch = None  # type: ts_sketch.Sketch
    self._processed_timelines: Set[int] = set()

  # pylint: disable=arguments-differ
  def SetUp(
      self,
      incident_id: str,
      sketch_id: Optional[int],
      analyzers: Optional[str],
      token_password: Optional[str],
      endpoint: Optional[str],
      username: Optional[str],
      password: Optional[str],
      wait_for_timelines: bool = False) -> None:
    """Setup a connection to a Timesketch server and create a sketch if needed.

    Args:
      incident_id (Optional[str]): Incident ID or reference. Used in sketch
          description.
      sketch_id (Optional[str]): Sketch ID to add the resulting timeline to.
          If not provided, a new sketch is created.
      analyzers (Optional[List[str]): If provided a list of analyzer names
          to run on the sketch after they've been imported to Timesketch.
      token_password (str): optional password used to decrypt the
          Timesketch credential storage. Defaults to an empty string since
          the upstream library expects a string value. An empty string means
          a password will be generated by the upstream library.
      endpoint: Timesketch server URL (e.g. http://localhost:5000/).
          Optional when token_password is provided.
      username: Timesketch username. Optional when token_password is provided.
      password: Timesketch password. Optional when token_password is provided.
      wait_for_timelines (bool): Whether to wait until timelines are processed
          in the Timesketch server or not.
    """
    self.wait_for_timelines = wait_for_timelines
    if endpoint and username and password:
      self.timesketch_api = ts_client.TimesketchApi(
          endpoint, username, password)
    elif token_password:
      self.logger.info('Using token password from recipe config.')
      self.timesketch_api = timesketch_utils.GetApiClient(
          self.state, token_password=token_password)
    else:
      self.logger.info(
          'No username / password or token password specified, creating config')
      self.timesketch_api = timesketch_utils.GetApiClient(self.state)

    if not self.timesketch_api:
      self.ModuleError(
          'Unable to get a Timesketch API client, try deleting the files '
          '~/.timesketchrc and ~/.timesketch.token',
          critical=True)
    self.incident_id = incident_id
    self.sketch_id = int(sketch_id) if sketch_id else 0
    self.sketch = None

    # Check that we have a timesketch session.
    if not (self.timesketch_api or self.timesketch_api.session):
      message = 'Could not connect to Timesketch server'
      self.ModuleError(message, critical=True)

    # If no sketch ID is provided through the CLI, attempt to get it from
    # attributes
    if not self.sketch_id:
      attributes = self.GetContainers(containers.TicketAttribute)
      self.sketch_id = timesketch_utils.GetSketchIDFromAttributes(attributes)

    # If we have a sketch ID, check that we can write to it and cache it.
    if self.sketch_id:
      self.sketch = self.timesketch_api.get_sketch(self.sketch_id)
      if 'write' not in self.sketch.my_acl:
        self.ModuleError(
            'No write access to sketch ID {0:d}, aborting'.format(
                self.sketch_id),
            critical=True)
      self.state.AddToCache('timesketch_sketch', self.sketch)
      self.sketch_id = self.sketch.id

    if analyzers:
      self._analyzers = [x.strip() for x in analyzers.split(',')]

    self.sketch = self.state.GetFromCache('timesketch_sketch')
    if not self.sketch and self.sketch_id:
      self.logger.info('Using exiting sketch: {0:d}'.format(self.sketch_id))
      self.sketch = self.timesketch_api.get_sketch(self.sketch_id)

    # Create the sketch if no sketch was stored in the cache.
    if not self.sketch:
      self.sketch = self._CreateSketch(incident_id=self.incident_id)
      self.sketch_id = self.sketch.id
      self.logger.info('New sketch created: {0:d}'.format(self.sketch_id))

    # register callback in timesketch module
    self.state.RegisterStreamingCallback(self.Process, containers.File)

  def _CreateSketch(
      self, incident_id: Optional[str] = None) -> ts_sketch.Sketch:
    """Creates a new Timesketch sketch.

    Args:
      incident_id (str): Incident ID to use sketch description.

    Returns:
      timesketch_api_client.Sketch: An instance of the sketch object.
    """
    if incident_id:
      sketch_name = 'Sketch for incident ID: ' + incident_id
    else:
      sketch_name = 'Untitled sketch'
    sketch_description = 'Sketch generated by dfTimewolf'

    sketch = self.timesketch_api.create_sketch(sketch_name, sketch_description)
    self.sketch_id = sketch.id
    if incident_id:
      sketch.add_attribute('incident_id', incident_id, ontology='text')
    self.state.AddToCache('timesketch_sketch', sketch)

    return sketch

  def _WaitForTimelines(self) -> None:
    """Waits for all timelines in a sketch to be processed.

    Runs analyzers on timelines that are ready.
    """
    sketch = self.timesketch_api.get_sketch(self.sketch_id)
    timelines = sketch.list_timelines()
    self.logger.info(
        f'Found {len(timelines)} timelines for sketch {self.sketch_id}')
    while timelines:
      for timeline in timelines:
        # if the timeline is is a final state, pop it from the list
        if timeline.status in ['fail', 'ready', 'timeout', 'archived']:
          timelines.remove(timeline)
          # if the timeline is ready, run the analyzers
          if timeline.status == 'ready' and (not timeline.id
                                             in self._processed_timelines):
            self._processed_timelines.add(timeline.id)
            self._RunAnalyzers(timeline.name)
        else:
          self.logger.info(f'Waiting for timeline {timeline.name} to be ready')
      time.sleep(30)

  def _RunAnalyzers(self, timeline_name: str) -> None:
    """Runs analyzers on a timeline."""
    if not self._analyzers:
      self.logger.info(
          'No analyzers to run on timeline {0:s}.'.format(timeline_name))
      return

    timeline = self.sketch.get_timeline(timeline_name=timeline_name)
    self.logger.info(
        "Running analyzers {0!s} on timeline {1:s}".format(
            self._analyzers, timeline_name))
    try:
      # By default run_analyzers() ignores analyzers that have already been run
      results: List[ts_analyzer.AnalyzerResult] = timeline.run_analyzers(
          analyzer_names=self._analyzers)
      if not results:
        self.logger.info(
            'No new analyzers to run on timeline {0:s}.'.format(timeline_name))
        return
      # Get the last result, which is the most recent run of the analyzers.
      result = results[-1]
      for analyzer_name, analyzer_status in result.status_dict.items():
        self.logger.debug(
            'Analyzer: {0:s} status: {1!s}'.format(
                analyzer_name, analyzer_status))
    except ts_error.UnableToRunAnalyzer as exception:
      self.ModuleError(
          'Unable to run analyzer: {0!s}'.format(exception), critical=False)

  # pytype: disable=signature-mismatch
  def Process(self, container: containers.File) -> None:
    """Executes a Timesketch export.

    Args:
      container (containers.File): A container holding a File to import."""

    recipe_name = self.state.recipe.get('name', 'no_recipe')
    rand = uuid.uuid4().hex[-5:]
    description = container.name
    if description:
      name = description.rpartition('.')[0]
      name = name if name else description
      name = name.replace(' ', '_').replace('-', '_')
      timeline_name = f'{recipe_name}_{name}'
    else:
      timeline_name = f'{recipe_name}'

    # Give each timeline a unique name
    timeline_name = f'{timeline_name}_{rand}'
    self.logger.info('Uploading {0:s} ...'.format(timeline_name))

    with importer.ImportStreamer() as streamer:
      streamer.set_sketch(self.sketch)
      streamer.set_timeline_name(timeline_name)

      path = container.path
      try:
        streamer.add_file(path)
      except RuntimeError as exception:
        self.ModuleError(
            'Unable to import {0:s}: {1!s}'.format(path, exception),
            critical=False)
      if streamer.response and container.description:
        streamer.timeline.description = container.description

  def GetThreadOnContainerType(self) -> Type[interface.AttributeContainer]:
    return containers.File

  def GetThreadPoolSize(self) -> int:
    return 5

  def PreProcess(self) -> None:
    pass

  def PostProcess(self) -> None:
    api_root = self.sketch.api.api_root
    host_url = api_root.partition('api/v1')[0]

    if self.wait_for_timelines:
      self._WaitForTimelines()

    sketch_url = '{0:s}sketches/{1:d}/'.format(host_url, self.sketch.id)
    message = 'Your Timesketch URL is: {0:s}'.format(sketch_url)
    self.PublishMessage(message)

    report_container = containers.Report(
        module_name='TimesketchExporter', text=message, text_format='markdown')
    self.StoreContainer(report_container)


modules_manager.ModulesManager.RegisterModule(TimesketchExporter)
