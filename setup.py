from setuptools import setup, find_packages

setup(name='jusi-extension-shell',
        version='0.0.1',
        description='Shell extension for Jusi, enabling shell magic and integration',
        long_desription='''Shell extension for jusi provides shell magic command,
            which unlike sh magic or exclamation mark cell provides bidirectional
            interaction between jupyter client and shell''',
        url='https://jusirepo',
        author='Nikita Pospelov',
        author_email='ponival@gmail.com',
        license='MIT',
        packages=find_packages(),
        install_requires=[
            'jusi>=0.0.1',
            ],
        entry_points={
            'jusi.extensions': [
                'shell = jusi_shell_extension:initialize',
                ],
            'jusi.display_handlers': [
                'application/vnd.jusi+shell = jusi_shell_extension.display_handler:ShellHandler',
                ],
            },
        classifiers=[
            'Programming Language :: Pyhton :: 3'
            'Framework :: Jupyter',
            'Framewark :: IPython',
            'Intended Audience :: Developers',
            'Intended Audience :: Science/Research',
            'License :: OSI Approved :: MIT License',
            'Operating System :: OS Independent',
            'Topic :: Software Developent :: Libraries',
            ],
        python_requieres='>=3.6',
        )

    


