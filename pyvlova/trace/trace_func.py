import inspect
import astor

from .trace import current_status


class TracableFuncMeta(type):
    _instances = dict()

    def __call__(cls, func):
        key = id(func)
        if key in cls._instances:
            return cls._instances[key]
        cls._instances[key] = super().__call__(func)
        return cls._instances[key]


class TracableCalcFunc(metaclass=TracableFuncMeta):
    def __init__(self, func):
        self.plain_func = func
        self.plain_src = inspect.getsource(func)
        # TODO: flatten source and add more info to source
        # self.func_ast = astor.code_to_ast(self.plain_src)
        # self.pretty_src = astor.to_source(self.func_ast)
        # self.pretty_func = utils.run_code(
        #     self.pretty_src, func.__globals__, utils.func_name(func)
        # )
        self.pretty_func = self.plain_func

    def __call__(self, *args, **kwargs):
        if current_status.tracing:
            print('Tracing', self.pretty_func.__name__)
            return self.pretty_func(*args, **kwargs)
        else:
            return self.plain_func(*args, **kwargs)

    def trace(self, *args, **kwargs):
        with current_status.start_tracing():
            return self(*args, **kwargs)