"""Tools for controlling experiment execution"""

# Authors: Dan McCloy <drmccloy@uw.edu>
#          Eric Larson <larsoner@uw.edu>
#
# License: BSD (3-clause)

import numpy as np
from scipy import linalg
import os
import threading
import warnings
from os import path as op
from functools import partial
from scipy.signal import resample
import pyglet
from pyglet import gl as GL

from ._utils import (get_config, verbose_dec, _check_pyglet_version, wait_secs,
                     running_rms, _sanitize, psylog, clock, date_str)
from ._tdt_controller import TDTController
from ._trigger_controllers import ParallelTrigger
from ._sound_controllers import PyoSound
from ._input_controllers import Keyboard, Mouse
from .visual import Text, Rectangle


class ExperimentController(object):
    """Interface for hardware control (audio, buttonbox, eye tracker, etc.)

    Parameters
    ----------
    exp_name : str
        Name of the experiment.
    audio_controller : str | dict | None
        If audio_controller is None, the type will be read from the system
        configuration file. If a string, can be 'pyo' or 'tdt', and the
        remaining audio parameters will be read from the machine configuration
        file. If a dict, must include a key 'TYPE' that is either 'pyo'
        or 'tdt'; the dict can contain other parameters specific to the TDT
        (see documentation for expyfun.TDTController).
    response_device : str | None
        Can only be 'keyboard' currently.  If None, the type will be read
        from the machine configuration file.
    stim_rms : float
        The RMS amplitude that the stimuli were generated at (strongly
        recommended to be 0.01).
    stim_fs : int | float
        The sampling frequency that the stimuli were generated with (samples
        per second).
    stim_db : float
        The desired dB SPL at which to play the stimuli.
    noise_db : float
        The desired dB SPL at which to play the dichotic noise.
    output_dir : str | None
        An absolute or relative path to a directory in which raw experiment
        data will be stored. If output_folder does not exist, it will be
        created. If None, no output data or logs will be saved
        (ONLY FOR TESTING!).
    window_size : list | array | None
        Window size to use. If list or array, it must have two elements.
        If None, the default will be read from the system config,
        falling back to [1920, 1080] if no system config is found.
    screen_num : int | None
        Screen to use. If None, the default will be read from the system
        config, falling back to 0 if no system config is found.
    full_screen : bool
        Should the experiment window be fullscreen?
    force_quit : list
        Keyboard key(s) to utilize as an experiment force-quit button. Can be
        a zero-element list for no force quit support. If None, defaults to
        ``['lctrl', 'rctrl']``.  Using ['escape'] is not recommended due to
        default handling of 'escape' in pyglet.
    participant : str | None
        If ``None``, a GUI will be used to acquire this information.
    session : str | None
        If ``None``, a GUI will be used to acquire this information.
    trigger_controller : str | None
        If ``None``, the type will be read from the system configuration file.
        If a string, must be 'dummy', 'parallel', or 'tdt'. Note that by
        default the mode is 'dummy', since setting up the parallel port
        can be a pain. Can also be a dict with entries 'type' ('parallel')
        and 'address' (e.g., '/dev/parport0').
    verbose : bool, str, int, or None
        If not None, override default verbose level (see expyfun.verbose).
    check_rms : str | None
        Method to use in checking stimulus RMS to ensure appropriate levels.
        Possible values are ``None``, ``wholefile``, and ``windowed`` (the
        default); see ``set_rms_checking`` for details.
    suppress_resamp : bool
        If ``True``, will suppress resampling of stimuli to the sampling
        frequency of the sound output device.

    Returns
    -------
    exp_controller : instance of ExperimentController
        The experiment control interface.

    Notes
    -----
    When debugging, it's useful to use the flush_logs() method to get
    information (based on the level of verbosity) printed to the console.
    """

    @verbose_dec
    def __init__(self, exp_name, audio_controller=None, response_device=None,
                 stim_rms=0.01, stim_fs=44100, stim_db=65, noise_db=45,
                 output_dir='rawData', window_size=None, screen_num=None,
                 full_screen=True, force_quit=None, participant=None,
                 monitor=None, trigger_controller=None, session=None,
                 verbose=None, check_rms='windowed', suppress_resamp=False):

        # Check Pyglet version for safety
        _check_pyglet_version(raise_error=True)

        # initialize some values
        self._stim_fs = stim_fs
        self._stim_rms = stim_rms
        self._stim_db = stim_db
        self._noise_db = noise_db
        self._stim_scaler = None
        self.set_rms_checking(check_rms)
        self._suppress_resamp = suppress_resamp
        # placeholder for extra actions to do on flip-and-play
        self._on_every_flip = []
        self._on_next_flip = []
        # placeholder for extra actions to run on close
        self._extra_cleanup_fun = []
        # some hardcoded parameters...

        # assure proper formatting for force-quit keys
        if force_quit is None:
            force_quit = ['lctrl', 'rctrl']
        elif isinstance(force_quit, (int, basestring)):
            force_quit = [str(force_quit)]
        if 'escape' in force_quit:
            psylog.warn('Expyfun: using "escape" as a force-quit key is not '
                        'recommended because it has special status in pyglet.')

        # set up timing
        self._master_clock = clock
        self._time_corrections = dict()
        self._time_correction_fxns = dict()

        # dictionary for experiment metadata
        self._exp_info = {'participant': participant, 'session': session,
                          'exp_name': exp_name, 'date': date_str()}

        # session start dialog, if necessary
        fixed_list = ['exp_name', 'date']  # things not user-editable in GUI
        for key, value in self._exp_info.iteritems():
            if key not in fixed_list and value is not None:
                if not isinstance(value, basestring):
                    raise TypeError('{} must be string or None'.format(value))
                fixed_list.append(key)

        if len(fixed_list) < len(self._exp_info):
            _get_items(self._exp_info, fixed=fixed_list, title=exp_name)

        #
        # initialize log file
        #
        if output_dir is not None:
            output_dir = op.abspath(output_dir)
            if not op.isdir(output_dir):
                os.mkdir(output_dir)
            basename = op.join(output_dir,
                               '{}_{}'.format(self._exp_info['participant'],
                                              self._exp_info['date']))
            self._log_file = basename + '.log'
            psylog.LogFile(self._log_file, level=psylog.INFO)
            # initialize data file
            self._data_file = open(basename + '.tab', 'a')
            self._data_file.write('# ' + str(self._exp_info) + '\n')
            self.write_data_line('event', 'value', 'timestamp')
        else:
            psylog.LogFile(None, level=psylog.info)
            self._data_file = None

        #
        # set up monitor
        #
        if monitor is None:
            monitor = dict()
            monitor['SCREEN_WIDTH'] = float(get_config('SCREEN_WIDTH', '51.0'))
            monitor['SCREEN_DISTANCE'] = float(get_config('SCREEN_DISTANCE',
                                               '48.0'))
            pix_size = get_config('SCREEN_SIZE_PIX', '1920,1080').split(',')
            pix_size = [int(p) for p in pix_size]
            monitor['SCREEN_SIZE_PIX'] = pix_size
        else:
            if not isinstance(monitor, dict):
                raise TypeError('monitor must be a dict')
            if not all([key in monitor for key in ['SCREEN_WIDTH',
                                                   'SCREEN_DISTANCE',
                                                   'SCREEN_SIZE_PIX']]):
                raise KeyError('monitor must have keys "SCREEN_WIDTH", '
                               '"SCREEN_DISTANCE", and "SCREEN_SIZE_PIX"')
        monitor['SCREEN_DPI'] = (monitor['SCREEN_SIZE_PIX'][0] /
                                 (monitor['SCREEN_WIDTH'] * 0.393701))
        monitor['SCREEN_HEIGHT'] = (monitor['SCREEN_WIDTH']
                                    / float(monitor['SCREEN_SIZE_PIX'][0])
                                    * float(monitor['SCREEN_SIZE_PIX'][1]))
        self._monitor = monitor

        #
        # parse audio controller
        #
        if audio_controller is None:
            audio_controller = {'TYPE': get_config('AUDIO_CONTROLLER',
                                                   'pyo')}
        elif isinstance(audio_controller, basestring):
            if audio_controller.lower() in ['pyo', 'psychopy', 'tdt']:
                audio_controller = {'TYPE': audio_controller.lower()}
            else:
                raise ValueError('audio_controller must be \'pyo\' or '
                                 '\'tdt\' (or a dict including \'TYPE\':'
                                 ' \'pyo\' or \'TYPE\': \'tdt\').')
        elif not isinstance(audio_controller, dict):
            raise TypeError('audio_controller must be a str or dict.')
        self._audio_type = audio_controller['TYPE'].lower()

        #
        # parse response device
        #
        if response_device is None:
            response_device = get_config('RESPONSE_DEVICE', 'keyboard')
        if response_device not in ['keyboard', 'tdt']:
            raise ValueError('response_device must be "keyboard", "tdt", or '
                             'None')
        self._response_device = response_device

        #
        # Initialize devices
        #

        # Audio (and for TDT, potentially keyboard)
        self._tdt_init = False
        if self._audio_type == 'tdt':
            psylog.info('Expyfun: Setting up TDT')
            as_kb = True if self._response_device == 'tdt' else False
            self._ac = TDTController(audio_controller, self, as_kb, force_quit)
            self._audio_type = self._ac.model
            self._tdt_init = True
        elif self._audio_type in ['pyo', 'psychopy']:
            if self._audio_type == 'psychopy':
                warnings.warn('psychopy is deprecated and will be removed in '
                              'version 1.2, use "pyo" instead for equivalent '
                              'functionality')
            self._ac = PyoSound(self, self.stim_fs)
        else:
            raise ValueError('audio_controller[\'TYPE\'] must be '
                             '\'psychopy\' or \'tdt\'.')
        # audio scaling factor; ensure uniform intensity across output devices
        self.set_stim_db(self._stim_db)
        self.set_noise_db(self._noise_db)

        if self._fs_mismatch:
            if self._suppress_resamp:
                psylog.warn('Mismatch between reported stim sample rate ({0}) '
                            'and device sample rate ({1}). Nothing will be '
                            'done about this because suppress_resamp is "True"'
                            '.'.format(self.stim_fs, self.fs))
            else:
                psylog.warn('Mismatch between reported stim sample rate ({0}) '
                            'and device sample rate ({1}). Experiment'
                            'Controller will resample for you, but that takes '
                            'a non-trivial amount of processing time and may '
                            'compromise your experimental timing and/or cause '
                            'artifacts.'.format(self.stim_fs, self.fs))

        #
        # set up visual window (must be done before keyboard and mouse)
        #
        psylog.info('Expyfun: Setting up screen')
        if window_size is None:
            window_size = get_config('WINDOW_SIZE', '1920,1080').split(',')
            window_size = [int(w) for w in window_size]
        if screen_num is None:
            screen_num = int(get_config('SCREEN_NUM', '0'))

        # open window and setup GL config
        self._setup_window(window_size, exp_name, full_screen, screen_num)

        # Keyboard
        if response_device == 'keyboard':
            self._response_handler = Keyboard(self, force_quit)
        if response_device == 'tdt':
            if not self._tdt_init:
                raise ValueError('response_device can only be "tdt" if '
                                 'tdt is used for audio')
            self._response_handler = self._ac

        #
        # set up trigger controller
        #
        if trigger_controller is None:
            trigger_controller = get_config('TRIGGER_CONTROLLER', 'dummy')
        if isinstance(trigger_controller, basestring):
            trigger_controller = dict(type=trigger_controller)
        psylog.info('Initializing {} triggering mode'
                    ''.format(trigger_controller['type']))
        if trigger_controller['type'] == 'tdt':
            if not self._tdt_init:
                raise ValueError('trigger_controller can only be "tdt" if '
                                 'tdt is used for audio')
            self._trigger_handler = self._ac
        elif trigger_controller['type'] in ['parallel', 'dummy']:
            if 'address' not in trigger_controller['type']:
                trigger_controller['address'] = get_config('TRIGGER_ADDRESS')
            out = ParallelTrigger(trigger_controller['type'],
                                  trigger_controller.get('address'))
            self._trigger_handler = out
            self._extra_cleanup_fun.append(self._trigger_handler.close)
        else:
            raise ValueError('trigger_controller type must be '
                             '"parallel", "dummy", or "tdt", not '
                             '{0}'.format(trigger_controller['type']))
        self._trigger_controller = trigger_controller['type']

        # other basic components
        self._mouse_handler = Mouse(self._win)

        # finish initialization
        psylog.info('Expyfun: Initialization complete')
        psylog.info('Expyfun: Subject: {0}'
                    ''.format(self._exp_info['participant']))
        psylog.info('Expyfun: Session: {0}'
                    ''.format(self._exp_info['session']))
        self.flush_logs()

    def __repr__(self):
        """Return a useful string representation of the experiment
        """
        string = ('<ExperimentController ({3}): "{0}" {1} ({2})>'
                  ''.format(self._exp_info['exp_name'],
                            self._exp_info['participant'],
                            self._exp_info['session'],
                            self._audio_type))
        return string

############################### SCREEN METHODS ###############################
    def screen_text(self, text, pos=[0, 0], h_align='center', v_align='center',
                    units='norm', color=[1, 1, 1], color_space='rgb',
                    height=0.1, wrap_width=1.5, h_flip=False, v_flip=False,
                    angle=0, opacity=1.0, contrast=1.0, name='', font='Arial'):
        """Show some text on the screen.

        Parameters
        ----------
        text : str
            The text to be rendered.
        pos : list | tuple
            x, y position of the text. In the default units (-1 to 1, with
            positive going up and right) the default is dead center (0, 0).
        h_align, v_align : str
            Horizontal/vertical alignment of the text relative to ``pos``
        units : str
            units for ``pos``.

        Returns
        -------
        Instance of visual.Text
        """
        scr_txt = Text(self, text)
        scr_txt.draw()
        self.call_on_next_flip(self.write_data_line, 'screen_text', text)
        return scr_txt

    def screen_prompt(self, text, max_wait=np.inf, min_wait=0, live_keys=None,
                      timestamp=False, clear_after=True):
        """Display text and (optionally) wait for user continuation

        Parameters
        ----------
        text : str | list
            The text to display. It will automatically wrap lines.
            If list, the prompts will be displayed sequentially.
        max_wait : float
            The maximum amount of time to wait before returning. Can be np.inf
            to wait until the user responds.
        min_wait : float
            The minimum amount of time to wait before returning. Useful for
            avoiding subjects missing instructions.
        live_keys : list | None
            The acceptable list of buttons or keys to use to advance the trial.
            If None, all buttons / keys will be accepted.  If an empty list,
            the prompt displays until max_wait seconds have passed.
        clear_after : bool
            If True, the screen will be cleared before returning.

        Returns
        -------
        pressed : tuple | str | None
            If ``timestamp==True``, returns a tuple ``(str, float)`` indicating
            the first key pressed and its timestamp (or ``(None, None)`` if no
            acceptable key was pressed between ``min_wait`` and ``max_wait``).
            If ``timestamp==False``, returns a string indicating the first key
            pressed (or ``None`` if no acceptable key was pressed).
        """
        if not isinstance(text, list):
            text = [text]
        if not all([isinstance(t, basestring) for t in text]):
            raise TypeError('text must be a string or list of strings')
        for t in text:
            self.screen_text(t)
            self.flip()
            out = self.wait_one_press(max_wait, min_wait, live_keys,
                                      timestamp)
        if clear_after:
            self.flip()
        return out

    def draw_background_color(self, color='black'):
        """Draw a solid background color

        Parameters
        ----------
        color : PsychoPy color
            The background color.

        Returns
        -------
        rect : instance of Rectangle
            The drawn Rectangle object.

        Notes
        -----
        This should be the first object drawn to a buffer, as it will
        cover any previsouly drawn objects.
        """
        # we go a little over here to be safe from round-off errors
        rect = Rectangle(self, width=2.1, height=2.1, fill_color=color)
        rect.draw()
        return rect

    def flip_and_play(self):
        """Flip screen, play audio, then run any "on-flip" functions.

        Returns
        -------
        flip_time : float
            The timestamp of the screen flip.

        Notes
        -----
        Order of operations is: screen flip, audio start, additional functions
        added with ``on_next_flip``, followed by functions added with
        ``on_every_flip``.
        """
        psylog.info('Expyfun: Flipping screen and playing audio')
        # ensure self._play comes first in list:
        self._on_next_flip = [self._play] + self._on_next_flip
        flip_time = self.flip()
        return flip_time

    def call_on_next_flip(self, function, *args, **kwargs):
        """Add a function to be executed on next flip only.

        Parameters
        ----------
        function : function | None
            The function to call. If ``None``, all the "on every flip"
            functions will be cleared.

        *args
        -----
        Function arguments.

        **kwargs
        --------
        Function keyword arguments.

        Notes
        -----
        See ``flip_and_play`` for order of operations. Can be called multiple
        times to add multiple functions to the queue.
        """
        if function is not None:
            function = partial(function, *args, **kwargs)
            self._on_next_flip.append(function)
        else:
            self._on_next_flip = []

    def call_on_every_flip(self, function, *args, **kwargs):
        """Add a function to be executed on every flip.

        Parameters
        ----------
        function : function | None
            The function to call. If ``None``, all the "on every flip"
            functions will be cleared.

        *args
        -----
        Function arguments.

        **kwargs
        --------
        Function keyword arguments.

        Notes
        -----
        See ``flip_and_play`` for order of operations. Can be called multiple
        times to add multiple functions to the queue.
        """
        if function is not None:
            function = partial(function, *args, **kwargs)
            self._on_every_flip.append(function)
        else:
            self._on_every_flip = []

    def _convert_units(self, verts, fro, to):
        """Convert between different screen units"""
        verts = np.atleast_2d(verts).copy()
        if verts.shape[0] != 2:
            raise RuntimeError('verts must have 2 rows')
        if fro not in self.unit_conversions:
            raise KeyError('unit_conversions does not have "{}"'.format(fro))
        if to not in self.unit_conversions:
            raise KeyError('unit_conversions does not have "{}"'.format(to))

        if fro == to:
            return verts.copy()

        # simplify by using two if neither is in normalized (native) units
        if 'norm' not in [to, fro]:
            # convert to normal
            verts = self._convert_units(verts, fro, 'norm')
            # convert from normal to dest
            verts = self._convert_units(verts, 'norm', to)
            return verts

        # figure out our actual transition, knowing one is 'norm'
        h_pix = self.size_pix[0]
        w_pix = self.size_pix[1]
        d_cm = self._monitor['SCREEN_DISTANCE']
        h_cm = self._monitor['SCREEN_HEIGHT']
        w_cm = self._monitor['SCREEN_WIDTH']
        if 'pix' in [to, fro]:
            if 'pix' == to:
                # norm to pixels
                x = np.array([[w_pix / 2., 0, -w_pix / 2.],
                              [0, h_pix / 2., -h_pix / 2.]])
            else:
                # pixels to norm
                x = np.array([[2. / w_pix, 0, -1.],
                              [0, 2. / h_pix, -1.]])
            verts = np.dot(x, np.r_[verts, np.ones(verts.shape[1])])
        elif 'deg' in [to, fro]:
            if 'deg' == to:
                # norm to deg
                x = np.arctan2(verts[0] / (w_cm / 2.), d_cm)
                y = np.arctan2(verts[1] / (h_cm / 2.), d_cm)
                verts = np.array([x, y])
                verts *= (180. / np.pi)
            else:
                # deg to norm
                verts *= (np.pi / 180.)
                x = d_cm * np.tan(verts[0])
                y = d_cm * np.tan(verts[1])
        else:
            raise KeyError('unknown conversion "{}" to "{}"'.format(fro, to))
        return verts

    @property
    def on_next_flip_functions(self):
        """Current stack of functions to be called on next flip."""
        return self._on_next_flip

    @property
    def on_every_flip_functions(self):
        """Current stack of functions called on every flip."""
        return self._on_every_flip

    @property
    def window(self):
        """Visual window handle."""
        return self._win

    @property
    def dpi(self):
        return self._monitor['SCREEN_DPI']

    @property
    def size_pix(self):
        return np.array([self._win.width, self._win.height])

############################### OPENGL METHODS ###############################
    def _setup_window(self, window_size, exp_name, full_screen, screen_num):
        config = GL.Config(depth_size=8, double_buffer=True,
                           stencil_size=0, stereo=False)
        self._win = pyglet.window.Window(width=window_size[0],
                                         height=window_size[1],
                                         caption=exp_name,
                                         fullscreen=full_screen,
                                         config=config,
                                         screen=screen_num,
                                         style='borderless')

        # with the context set up, do GL stuff
        GL.glClearColor(0.0, 0.0, 0.0, 1.0)
        GL.glClearDepth(1.0)
        GL.glViewport(0, 0, int(self.size_pix[0]), int(self.size_pix[1]))
        GL.glMatrixMode(GL.GL_PROJECTION)  # Reset The Projection Matrix
        GL.glLoadIdentity()
        GL.gluOrtho2D(-1, 1, -1, 1)
        GL.glMatrixMode(GL.GL_MODELVIEW)  # Reset The Projection Matrix
        GL.glLoadIdentity()
        GL.glDisable(GL.GL_DEPTH_TEST)
        GL.glEnable(GL.GL_BLEND)
        GL.glBlendFunc(GL.GL_SRC_ALPHA, GL.GL_ONE_MINUS_SRC_ALPHA)
        GL.glShadeModel(GL.GL_SMOOTH)  # Color Shading (FLAT or SMOOTH)
        GL.glEnable(GL.GL_POINT_SMOOTH)
        GL.glClear(GL.GL_COLOR_BUFFER_BIT)

    def flip(self):
        """Flip screen, then run any "on-flip" functions.

        Returns
        -------
        flip_time : float
            The timestamp of the screen flip.

        Notes
        -----
        Order of operations is: screen flip, audio start, additional functions
        added with ``on_every_flip``, followed by functions added with
        ``on_next_flip``.
        """
        psylog.info('Expyfun: Flipping screen')
        call_list = self._on_next_flip + self._on_every_flip
        GL.glTranslatef(0.0, 0.0, -5.0)
        #for dispatcher in self._eventDispatchers:
        #    dispatcher.dispatch_events()
        self._win.dispatch_events()
        self._win.flip()
        GL.glLoadIdentity()
        #waitBlanking
        GL.glBegin(GL.GL_POINTS)
        GL.glColor4f(0, 0, 0, 0)
        GL.glVertex2i(10, 10)
        GL.glEnd()
        GL.glFinish()
        flip_time = clock()
        for function in call_list:
            function()
        GL.glClear(GL.GL_COLOR_BUFFER_BIT)
        self.write_data_line('flip', flip_time)
        self._on_next_flip = []
        return flip_time

############################ KEYPRESS METHODS ############################
    def listen_presses(self):
        """Start listening for keypresses.
        """
        self._response_handler.listen_presses()

    def get_presses(self, live_keys=None, timestamp=True, relative_to=None):
        """Get the entire keyboard / button box buffer.

        Parameters
        ----------
        live_keys : list | None
            List of strings indicating acceptable keys or buttons. Other data
            types are cast as strings, so a list of ints will also work.
            live_keys=None accepts all keypresses.
        timestamp : bool
            Whether the keypress should be timestamped. If True, returns the
            button press time relative to the value given in ``relative_to``.
        relative_to : None | float
            A time relative to which timestamping is done. Ignored if
            timestamp==False.  If ``None``, timestamps are relative to the time
            ``listen_presses`` was last called.
        """
        return self._response_handler.get_presses(live_keys, timestamp,
                                                  relative_to)

    def wait_one_press(self, max_wait=np.inf, min_wait=0.0, live_keys=None,
                       timestamp=True, relative_to=None):
        """Returns only the first button pressed after min_wait.

        Parameters
        ----------
        max_wait : float
            Duration after which control is returned if no key is pressed.
        min_wait : float
            Duration for which to ignore keypresses (force-quit keys will
            still be checked at the end of the wait).
        live_keys : list | None
            List of strings indicating acceptable keys or buttons. Other data
            types are cast as strings, so a list of ints will also work.
            ``live_keys=None`` accepts all keypresses.
        timestamp : bool
            Whether the keypress should be timestamped. If ``True``, returns
            the button press time relative to the value given in
            ``relative_to``.
        relative_to : None | float
            A time relative to which timestamping is done. Ignored if
            ``timestamp==False``.  If ``None``, timestamps are relative to the
            time ``wait_one_press`` was called.

        Returns
        -------
        pressed : tuple | str | None
            If ``timestamp==True``, returns a tuple (str, float) indicating the
            first key pressed and its timestamp (or ``(None, None)`` if no
            acceptable key was pressed between ``min_wait`` and ``max_wait``).
            If ``timestamp==False``, returns a string indicating the first key
            pressed (or ``None`` if no acceptable key was pressed).
        """
        return self._response_handler.wait_one_press(max_wait, min_wait,
                                                     live_keys, timestamp,
                                                     relative_to)

    def wait_for_presses(self, max_wait, min_wait=0.0, live_keys=None,
                         timestamp=True, relative_to=None):
        """Returns all button presses between min_wait and max_wait.

        Parameters
        ----------
        max_wait : float
            Duration after which control is returned.
        min_wait : float
            Duration for which to ignore keypresses (force-quit keys will
            still be checked at the end of the wait).
        live_keys : list | None
            List of strings indicating acceptable keys or buttons. Other data
            types are cast as strings, so a list of ints will also work.
            ``live_keys=None`` accepts all keypresses.
        timestamp : bool
            Whether the keypresses should be timestamped. If ``True``, returns
            the button press time relative to the value given in
            ``relative_to``.
        relative_to : None | float
            A time relative to which timestamping is done. Ignored if
            ``timestamp`` is ``False``.  If ``None``, timestamps are relative
            to the time ``wait_for_presses`` was called.

        Returns
        -------
        presses : list
            If timestamp==False, returns a list of strings indicating which
            keys were pressed. Otherwise, returns a list of tuples
            (str, float) of keys and their timestamps. If no keys are pressed,
            returns [].
        """
        return self._response_handler.wait_for_presses(max_wait, min_wait,
                                                       live_keys, timestamp,
                                                       relative_to)

    def _log_presses(self, pressed):
        """Write key presses to data file.
        """
        # This function will typically be called by self._response_handler
        # after it retrieves some button presses
        for key, stamp in pressed:
            self.write_data_line('keypress', key, stamp)

############################# MOUSE METHODS ##################################
    def get_mouse_position(self, units='pix'):
        """Mouse position in screen coordinates

        Parameters
        ----------
        units : str
            Either ``'pix'`` or ``'norm'`` for the type of units to return.

        Returns
        -------
        position : ndarray
            The mouse position.
        """
        if not units in ['pix', 'norm']:
            raise RuntimeError('must request units in "pix" or "norm"')
        pos = np.array(self._mouse_handler.pos)
        if units == 'pix':
            pos *= self.size_pix / 2.
            pos += self.size_pix / 2.
        return pos

    def toggle_cursor(self, visibility, flip=False):
        """Show or hide the mouse

        Parameters
        ----------
        visibility : bool
            If True, show; if False, hide.
        """
        self._mouse_handler.set_visible(visibility)
        if flip:
            self.flip()

################################ AUDIO METHODS ###############################
    def start_noise(self):
        """Start the background masker noise."""
        self._ac.start_noise()

    def stop_noise(self):
        """Stop the background masker noise."""
        self._ac.stop_noise()

    def clear_buffer(self):
        """Clear audio data from the audio buffer."""
        self._ac.clear_buffer()
        psylog.info('Expyfun: Buffer cleared')

    def load_buffer(self, samples):
        """Load audio data into the audio buffer.

        Parameters
        ----------
        samples : np.array
            Audio data as floats scaled to (-1,+1), formatted as an Nx1 or Nx2
            numpy array with dtype float32.
        """
        samples = self._validate_audio(samples) * self._stim_scaler
        psylog.info('Expyfun: Loading {} samples to buffer'
                    ''.format(samples.size))
        self._ac.load_buffer(samples)

    def _play(self):
        """Play the audio buffer.
        """
        psylog.debug('Expyfun: playing audio')
        self._ac.play()
        self.write_data_line('play')

    def stop(self):
        """Stop audio buffer playback and reset cursor to beginning of buffer.
        """
        self._ac.stop()
        self.write_data_line('stop')
        psylog.info('Expyfun: Audio stopped and reset.')

    def set_noise_db(self, new_db):
        """Set the level of the background noise.
        """
        # Noise is always generated at an RMS of 1
        self._ac.set_noise_level(self._update_sound_scaler(new_db, 1.0))
        self._noise_db = new_db

    def set_stim_db(self, new_db):
        """Set the level of the stimuli.
        """
        self._stim_db = new_db
        self._stim_scaler = self._update_sound_scaler(new_db, self._stim_rms)
        # not immediate: new value is applied on the next load_buffer call

    def _update_sound_scaler(self, desired_db, orig_rms):
        """Calcs coefficient ensuring stim ampl equivalence across devices.
        """
        exponent = (-(_get_dev_db(self._audio_type) - desired_db) / 20.0)
        return (10 ** exponent) / float(orig_rms)

    def _validate_audio(self, samples):
        """Converts audio sample data to the required format.

        Parameters
        ----------
        samples : list | array
            The audio samples.  Mono sounds will be converted to stereo.

        Returns
        -------
        samples : numpy.array(dtype='float32')
            The correctly formatted audio samples.
        """
        # check data type
        if type(samples) is list:
            samples = np.asarray(samples, dtype='float32')
        elif samples.dtype != 'float32':
            samples = np.float32(samples)

        # check values
        if np.max(np.abs(samples)) > 1:
            raise ValueError('Sound data exceeds +/- 1.')
            # samples /= np.max(np.abs(samples),axis=0)

        # check dimensionality
        if samples.ndim > 2:
            raise ValueError('Sound data has more than two dimensions.')

        # check shape
        if samples.ndim == 2 and min(samples.shape) > 2:
            raise ValueError('Sound data has more than two channels.')
        elif len(samples.shape) == 2 and samples.shape[0] <= 2:
            samples = samples.T

        # resample if needed
        if self._fs_mismatch and not self._suppress_resamp:
            psylog.warn('Resampling {} seconds of audio'
                        ''.format(round(len(samples) / self.stim_fs), 2))
            num_samples = len(samples) * self.fs / float(self.stim_fs)
            samples = resample(samples, int(num_samples), window='boxcar')

        # make stereo if not already
        if samples.ndim == 1:
            samples = np.array((samples, samples)).T
        elif 1 in samples.shape:
            samples = samples.ravel()
            samples = np.array((samples, samples)).T

        # check RMS
        if self._check_rms is not None:
            chans = [samples[:, x] for x in range(samples.shape[1])]
            if self._check_rms == 'wholefile':
                chan_rms = [np.sqrt(np.mean(x ** 2)) for x in chans]
                max_rms = max(chan_rms)
            else:  # 'windowed'
                win_length = int(self.fs * 0.01)  # 10ms running window
                chan_rms = [running_rms(x, win_length) for x in chans]
                max_rms = max([max(x) for x in chan_rms])
            if max_rms > 2 * self._stim_rms:
                warn_string = ('Stimulus max RMS ({}) exceeds stated RMS ({}) '
                               'by more than 6 dB.'.format(max_rms,
                                                           self._stim_rms))
                psylog.warn(warn_string)
                raise UserWarning(warn_string)
            elif max_rms < 0.5 * self._stim_rms:
                warn_string = ('Stimulus max RMS is less than stated RMS by '
                               'more than 6 dB.')
                psylog.warn(warn_string)
                # raise UserWarning(warn_string)

        # always prepend a zero to deal with TDT reset of buffer position
        samples = np.r_[np.atleast_2d([0.0, 0.0]), samples]
        return np.ascontiguousarray(samples)

    def set_rms_checking(self, check_rms):
        """Set the RMS checking flag.

        Parameters
        ----------
        check_rms : str | None
            Method to use in checking stimulus RMS to ensure appropriate
            levels. ``'windowed'`` uses a 10ms window to find the max RMS in
            each channel and checks to see that it is within 6 dB of the stated
            ``stim_rms``.  ``'wholefile'`` checks the RMS of the stimulus as a
            whole, while ``None`` disables RMS checking.
        """
        if check_rms not in [None, 'wholefile', 'windowed']:
            raise ValueError('check_rms must be one of "wholefile", "windowed"'
                             ', or None.')
        self._check_rms = check_rms

################################ OTHER METHODS ###############################
    def write_data_line(self, event_type, value=None, timestamp=None):
        """Add a line of data to the output CSV.

        Parameters
        ----------
        event_type : str
            Type of event (e.g., keypress, screen flip, etc.)
        value : None | str
            Anything that can be cast to a string is okay here.
        timestamp : float | None
            The timestamp when the event occurred.  If ``None``, will use the
            time the data line was written from the master clock.

        Notes
        -----
        Writing a data line causes the file to be flushed, which may take
        some time (although it usually shouldn't), so avoid calling during
        critical timing periods.
        """
        if timestamp is None:
            timestamp = self._master_clock()
        ll = '\t'.join(_sanitize(x) for x in [timestamp, event_type,
                                              value]) + '\n'
        if self._data_file is not None:
            self._data_file.write(ll)
            self._data_file.flush()  # make sure it's actually written out

    def _get_time_correction(self, clock_type):
        """Clock correction (seconds) for win.flip().
        """
        time_correction = (self._master_clock() -
                           self._time_correction_fxns[clock_type]())
        if clock_type not in self._time_corrections:
            self._time_corrections[clock_type] = time_correction

        diff = time_correction - self._time_corrections[clock_type]
        if np.abs(diff) > 10e-6:
            psylog.warn('Expyfun: drift of > 10 microseconds ({}) '
                        'between {} clock and EC master clock.'
                        ''.format(round(diff * 10e6), clock_type))
        psylog.debug('Expyfun: time correction between {} clock and EC '
                     'master clock is {}. This is a change of {}.'
                     ''.format(clock_type, time_correction, time_correction
                               - self._time_corrections[clock_type]))
        return time_correction

    def wait_secs(self, *args, **kwargs):
        """Wait a specified number of seconds.

        Parameters
        ----------
        secs : float
            Number of seconds to wait.
        hog_cpu_time : float
            Amount of CPU time to hog. See Notes.

        Notes
        -----
        See the wait_secs() function.
        """
        wait_secs(*args, **kwargs)

    def wait_until(self, timestamp):
        """Wait until the given time is reached.

        Parameters
        ----------
        timestamp : float
            A time to wait until, evaluated against the experiment master
            clock.

        Returns
        -------
        remaining_time : float
            The difference between ``timestamp`` and the time ``wait_until``
            was called.

        Notes
        -----
        Unlike ``wait_secs``, there is no guarantee of precise timing with this
        function. It is the responsibility of the user to do choose a
        reasonable timestamp (or equivalently, do a reasonably small amount of
        processing prior to calling ``wait_until``).
        """
        time_left = timestamp - self._master_clock()
        if time_left < 0:
            psylog.warn('wait_until was called with a timestamp ({}) that had '
                        'already passed {} seconds prior.'
                        ''.format(timestamp, -time_left))
        else:
            wait_secs(time_left)
        return time_left

    def stamp_triggers(self, trigger_list, delay=0.03):
        """Stamp experiment ID triggers

        Parameters
        ----------
        trigger_list : list
            List of numbers to stamp.
        delay : float
            Delay to use between sequential triggers.

        Notes
        -----
        Depending on how EC was initialized, stamping could be done
        using different pieces of hardware (e.g., parallel port or TDT).
        Also note that it is critical that the input is a list, and
        that all elements are integers. No input checking is done to
        ensure responsiveness.

        Also note that control will not be returned to the script until
        the stamping is complete.
        """
        self._trigger_handler.stamp_triggers(trigger_list, delay)
        psylog.exp('Expyfun: Stamped: ' + str(trigger_list))

    def flush_logs(self):
        """Flush logs (useful for debugging)
        """
        # pyflakes won't like this, but it's better here than outside class
        psylog.flush()

    def close(self):
        """Close all connections in experiment controller.
        """
        self.__exit__(None, None, None)

    def __enter__(self):
        psylog.debug('Expyfun: Entering')
        return self

    def __exit__(self, err_type, value, traceback):
        """
        Notes
        -----
        err_type, value and traceback will be None when called by self.close()
        """
        psylog.debug('Expyfun: Exiting cleanly')

        # do external cleanups
        cleanup_actions = [self.stop_noise, self.stop,
                           self._ac.halt, self._win.close]
        if self._data_file is not None:
            cleanup_actions.append(self._data_file.close)
        cleanup_actions.extend(self._extra_cleanup_fun)
        for action in cleanup_actions:
            try:
                action()
            except Exception as exc:
                print exc
                continue

        # clean up our API
        try:
            self._win.close()
            #logging.flush()
            for thisThread in threading.enumerate():
                if hasattr(thisThread, 'stop') and \
                        hasattr(thisThread, 'running'):
                    # this is one of our event threads - kill it and wait
                    thisThread.stop()
                    while thisThread.running == 0:
                        pass  # wait until it has properly finished polling
        except Exception as exc:
            print exc

        if any([x is not None for x in (err_type, value, traceback)]):
            raise err_type, value, traceback

############################# READ-ONLY PROPERTIES ###########################
    @property
    def fs(self):
        """Playback frequency of the audio controller (samples / second).
        """
        return self._ac.fs  # not user-settable

    @property
    def stim_fs(self):
        """Sampling rate at which the stimuli were generated.
        """
        return self._stim_fs  # not user-settable

    @property
    def stim_db(self):
        """Sound power in dB of the stimuli.
        """
        return self._stim_db  # not user-settable

    @property
    def noise_db(self):
        """Sound power in dB of the background noise.
        """
        return self._noise_db  # not user-settable

    @property
    def current_time(self):
        """Timestamp from the experiment master clock.
        """
        return self._master_clock()

    @property
    def _fs_mismatch(self):
        """Quantify if sample rates substantively differ.
        """
        return not np.allclose(self.stim_fs, self.fs, rtol=0, atol=0.5)


def _get_items(d, fixed, title):
    """Helper to get items for an experiment"""
    print title
    for key, val in d.iteritems():
        if key in fixed:
            print '{0}: {1}'.format(key, val)
        else:
            d[key] = raw_input('{0}: '.format(key))


def _get_dev_db(audio_controller):
    """Selects device-specific amplitude to ensure equivalence across devices.
    """
    if audio_controller == 'RM1':
        return 108  # this is approx w/ knob @ 12 o'clock (knob not detented)
    elif audio_controller == 'RP2':
        return 108
    elif audio_controller == 'RZ6':
        return 114
    elif audio_controller in ['pyo', 'psychopy']:
        return 90  # TODO: this value not yet calibrated, may vary by system
    else:
        psylog.warn('Unknown audio controller: stim scaler may not work '
                    'correctly. You may want to remove your headphones if this'
                    ' is the first run of your experiment.')
        return 90  # for untested TDT models
