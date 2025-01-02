from IPython.display import display

def shell(line, cell):
    class BashReprable:
        def __init__(self, data):
            self.data = data
        def _repr_mimebundle_(self, include=None, exclude=None):
            return {
                    'application/vnd.jusi+shell': self.data,
                    'text/plain': self.data
                    }
    display(BashReprable(cell))

def register(ipython_shell):
    ipython_shell.register_magic_function(shell, 'cell', 'shell')

