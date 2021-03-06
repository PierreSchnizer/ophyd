import time
import functools
import queue
import threading


class _CallbackThread(threading.Thread):
    'A queue-based callback dispatcher thread'

    def __init__(self, name, *, dispatcher, logger, context,
                 stop_event, timeout, callback_queue=None, daemon=True):
        super().__init__(name=name, daemon=daemon)
        self.context = context
        self.current_callback = None
        self.dispatcher = dispatcher
        self.logger = logger
        self.stop_event = stop_event
        self.timeout = timeout

        if callback_queue is None:
            callback_queue = queue.Queue()

        self.queue = callback_queue

    def __repr__(self):
        return '<{} qsize={}>'.format(self.__class__.__name__,
                                      self.queue.qsize())

    def run(self):
        '''The dispatcher itself'''
        self.logger.debug('Callback thread %s started', self.name)
        self.attach_context()

        while not self.stop_event.is_set():
            try:
                callback, args, kwargs = self.queue.get(True, self.timeout)
            except queue.Empty:
                ...
            else:
                try:
                    self.current_callback = (callback.__name__, kwargs.get('pvname'))
                    callback(*args, **kwargs)
                except Exception as ex:
                    self.logger.exception(
                        'Exception occurred during callback %r (pvname=%r)',
                        callback, kwargs.get('pvname')
                    )

        self.detach_context()
        self.logger.debug('Callback thread %s exiting', self.name)

    def attach_context(self):
        self.logger.debug('Callback thread %s attaching to context %s',
                          self.name, self.context)

    def detach_context(self):
        self.logger.debug('Callback thread %s detaching from context %s',
                          self.name, self.context)
        self.context = None


class EventDispatcher:
    def __init__(self, *, context, logger, timeout=0.1,
                 thread_class=_CallbackThread, debug_monitor=False,
                 utility_threads=4):
        self._threads = {}
        self._thread_class = thread_class
        self._timeout = timeout

        # The dispatcher thread will stop if this event is set
        self._stop_event = threading.Event()
        self.context = context
        self.logger = logger
        self._utility_threads = [f'util{i}' for i in range(utility_threads)]
        self._utility_queue = queue.Queue()

        self._start_thread(name='metadata')
        self._start_thread(name='monitor')
        self._start_thread(name='get_put')

        for name in self._utility_threads:
            self._start_thread(name=name, callback_queue=self._utility_queue)

        if debug_monitor:
            self._debug_monitor_thread = threading.Thread(
                target=self._debug_monitor,
                name='debug_monitor',
                daemon=True)
            self._debug_monitor_thread.start()

    def _debug_monitor(self, interval=0.01):
        while not self._stop_event.is_set():
            queue_sizes = [(name, thread.queue.qsize(), thread.current_callback)
                           for name, thread in sorted(self._threads.items())
                           ]
            status = [
                '{name}={qsize} ({cb})'.format(name=name, qsize=qsize, cb=cb)
                for name, qsize, cb in queue_sizes
                if qsize > 0
            ]
            if status:
                print('Dispatcher debug:', ' / '.join(status))
            time.sleep(interval)

    def __repr__(self):
        threads = [repr(thread) for thread in self._threads.values()]
        return '<{} threads={}>'.format(self.__class__.__name__, threads)

    def is_alive(self):
        return any(thread.is_alive() for thread in self.threads.values()
                   if thread is not None)

    @property
    def stop_event(self):
        return self._stop_event

    @property
    def timeout(self):
        return self._timeout

    @property
    def threads(self):
        return dict(self._threads)

    def stop(self):
        '''Stop the dispatcher threads and re-enable normal callbacks'''
        self._stop_event.set()
        for attr, thread in list(self._threads.items()):
            if thread is not None:
                thread.join()

        self._threads.clear()

    def schedule_utility_task(self, callback, *args, **kwargs):
        'Schedule `callback` with the given args and kwargs in a util thread'
        self._utility_queue.put((callback, args, kwargs))

    def _start_thread(self, name, *, callback_queue=None):
        'Start dispatcher thread by name'
        self._threads[name] = self._thread_class(name=name, dispatcher=self,
                                                 stop_event=self._stop_event,
                                                 timeout=self.timeout,
                                                 context=self.context,
                                                 logger=self.logger,
                                                 daemon=True,
                                                 callback_queue=callback_queue)
        self._threads[name].start()


def wrap_callback(dispatcher, event_type, callback):
    'Wrap a callback for usage with the dispatcher'
    if callback is None or getattr(callback, '_wrapped_callback', False):
        return callback

    assert event_type in dispatcher._threads
    callback_queue = dispatcher._threads[event_type].queue

    @functools.wraps(callback)
    def wrapped(*args, **kwargs):
        callback_queue.put((callback, args, kwargs))

    wrapped._wrapped_callback = True
    return wrapped
