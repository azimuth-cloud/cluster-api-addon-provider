import base64
import json
import typing as t

import jinja2

from pydantic.json import pydantic_encoder

import yaml

from . import utils


class Loader:
    """
    Class for returning objects created by rendering YAML templates from this package.
    """

    def __init__(self, **globals):
        # We only need to render strings and want to shut templates in a sandbox
        # So we use the base loader which doesn't have an implementation for including templates
        self._env = jinja2.Environment(loader=jinja2.BaseLoader(), autoescape=False)
        self._env.globals.update(globals)
        self._env.filters.update(
            mergeconcat=utils.mergeconcat,
            fromyaml=yaml.safe_load,
            # In order to benefit from correct serialisation of a variety of objects,
            # including Pydantic models, generic iterables and generic mappings, we go
            # via JSON with the Pydantic encoder
            toyaml=lambda obj: yaml.safe_dump(
                json.loads(json.dumps(obj, default=pydantic_encoder))
            ),
            b64encode=lambda data: base64.b64encode(data).decode(),
            b64decode=lambda data: base64.b64decode(data).decode(),
        )

    def render_string(self, template_str: str, **params: t.Any) -> str:
        """
        Render the given template string with the given params and return the result as
        a string.

        By default, this uses the safe environment which does not have access to templates.
        """
        return self._env.from_string(template_str).render(**params)

    def yaml_string(self, template_str: str, **params: t.Any) -> t.Dict[str, t.Any]:
        """
        Render the given template string with the given params, parse the result as YAML
        and return the resulting object.
        """
        return yaml.safe_load(self.render_string(template_str, **params))

    def yaml_string_all(self, template_str: str, **params: t.Any) -> t.Dict[str, t.Any]:
        """
        Render the given template string with the given params, parse the result as YAML
        and return the resulting objects.
        """
        return yaml.safe_load_all(self.render_string(template_str, **params))
