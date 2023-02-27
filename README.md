# EduLint

[![Pypi](https://img.shields.io/pypi/v/edulint)](https://pypi.org/project/edulint/)
![Python versions](https://img.shields.io/badge/python-%3E%3D%203.8-blue)
![Tests](https://img.shields.io/github/actions/workflow/status/GiraffeReversed/edulint/test.yaml)
[![ReadTheDocs](https://img.shields.io/readthedocs/edulint)](https://edulint.readthedocs.io/)
[![Docker image](https://img.shields.io/docker/image-size/edulint/edulint-cli/latest?label=Docker%20image%20size)](https://hub.docker.com/r/edulint/edulint-cli)

EduLint is a Python linter aimed at helping beginning programmers improve their coding style. At present moment, it integrates flake8 and pylint, with some tweaks to their default configuration.

The repository contains both the linter itself. For ease of use for the beginning programmers, there is a web version running at [edulint.rechtackova.cz](https://edulint.rechtackova.cz/).

You can install the latest release with pip:

```
python<version> -m pip install edulint
```


Once installed, you can run it as a python module:

```
python<the-same-version> -m edulint <file-to-lint>
```

Read the [documentation](https://edulint.readthedocs.io/) for more options and configuration details.


#### Python version

Supported: >= 3.8

_3.7 mostly works, but can fail in edge cases due to different parsing between package typed-ast (<=3.7) and pythons native ast (>=3.8). We discovered those using the existing test suite._

#### Docker usage

Don't want to install Python and Edulint as a python package? You can also run it using Docker.

```sh
docker run -v ${PWD}:/app edulint/edulint-cli some_file.py
```

