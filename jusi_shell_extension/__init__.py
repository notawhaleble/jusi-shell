def initialize(ipython_shell):
    from . import shell_magic
    shell_magic.register(ipython_shell)
