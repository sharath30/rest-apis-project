from flask import jsonify, request, current_app
from marshmallow import missing, EXCLUDE, ValidationError, fields
import collections
from functools import wraps
import inspect


# Find the stack on which we want to store the database connection.
# Starting with Flask 0.9, the _app_ctx_stack is the correct one,
# before that we need to use the _request_ctx_stack.
try:
    from flask import _app_ctx_stack as stack
except ImportError:
    from flask import _request_ctx_stack as stack


class Smores(object):
    def __init__(self, app=None):
        self.app = app
        if app is not None:
            self.init_app(app)

    def init_app(self, app):
        app.config.setdefault('SMORES_CACHE_API_DOCS', True)
        app.config.setdefault('SMORES_API_DOCS_RULE', '/api_docs')
        app.config.setdefault('SMORES_RECURSION_DEPTH', 5)

        if app.config.get('SMORES_CACHE_API_DOCS'):
            def api_docs():
                api_docs = getattr(app, '_api_docs', {})
                return jsonify(api_docs)

            app.add_url_rule(app.config['SMORES_API_DOCS_RULE'], 'smores_api_docs', api_docs)

            @app.before_first_request
            def cache_api_docs():
                app._api_docs = {}
                possible_methods = ['GET', 'POST', 'PUT', 'PATCH', 'DELETE']
                for rule in app.url_map.iter_rules():
                    view_func = app.view_functions[rule.endpoint]
                    if getattr(view_func, '_uses_smores', False):
                        methods = [x for x in possible_methods if x in rule.methods]
                        doc_dict = {}
                        if view_func.__doc__:
                            stripped_docstring = ' '.join((x.strip() for x in view_func.__doc__.strip().split('\n')))
                            doc_dict['description'] = stripped_docstring
                        if getattr(view_func, '_input_schema', None):
                            doc_dict['inputs'] = make_schema_dict(view_func._input_schema, max_depth=app.config['SMORES_RECURSION_DEPTH'])
                            if hasattr(view_func._input_schema, 'smores_example'):
                                doc_dict['example'] = view_func._input_schema.smores_example
                        if getattr(view_func, '_output_schema', None):
                            doc_dict['outputs'] = make_schema_dict(view_func._output_schema, is_input=False, max_depth=app.config['SMORES_RECURSION_DEPTH'])
                        for method in methods:
                            try:
                                app._api_docs[rule.rule][method] = doc_dict
                            except KeyError:
                                app._api_docs[rule.rule] = {method: doc_dict}


REQUEST_ATTR_MAP = {
    'path': 'view_args',
    'query': 'args',
    'header': 'headers',
    'cookie': 'cookies'
}


class CaseInsensitiveDict(collections.MutableMapping):
    """
    A case-insensitive ``dict``-like object. (Pasted from the requests library to avoid a dependency)

    Implements all methods and operations of
    ``collections.MutableMapping`` as well as dict's ``copy``. Also
    provides ``lower_items``.

    All keys are expected to be strings. The structure remembers the
    case of the last key to be set, and ``iter(instance)``,
    ``keys()``, ``items()``, ``iterkeys()``, and ``iteritems()``
    will contain case-sensitive keys. However, querying and contains
    testing is case insensitive::

        cid = CaseInsensitiveDict()
        cid['Accept'] = 'application/json'
        cid['aCCEPT'] == 'application/json'  # True
        list(cid) == ['Accept']  # True

    For example, ``headers['content-encoding']`` will return the
    value of a ``'Content-Encoding'`` response header, regardless
    of how the header name was originally stored.

    If the constructor, ``.update``, or equality comparison
    operations are given keys that have equal ``.lower()``s, the
    behavior is undefined.

    """
    def __init__(self, data=None, **kwargs):
        self._store = collections.OrderedDict()
        if data is None:
            data = {}
        self.update(data, **kwargs)

    def __setitem__(self, key, value):
        # Use the lowercased key for lookups, but store the actual
        # key alongside the value.
        self._store[key.lower()] = (key, value)

    def __getitem__(self, key):
        return self._store[key.lower()][1]

    def __delitem__(self, key):
        del self._store[key.lower()]

    def __iter__(self):
        return (casedkey for casedkey, mappedvalue in self._store.values())

    def __len__(self):
        return len(self._store)

    def lower_items(self):
        """Like iteritems(), but with all lowercase keys."""
        return (
            (lowerkey, keyval[1])
            for (lowerkey, keyval)
            in self._store.items()
        )

    def __eq__(self, other):
        if isinstance(other, collections.Mapping):
            other = CaseInsensitiveDict(other)
        else:
            return NotImplemented
        # Compare insensitively
        return dict(self.lower_items()) == dict(other.lower_items())

    # Copy is required
    def copy(self):
        return CaseInsensitiveDict(self._store.values())

    def __repr__(self):
        return str(dict(self.items()))


def make_schema_dict(schema, is_input=True, current_depth=0, max_depth=5):
    if inspect.isclass(schema):
        schema = schema()
    schema_dict = {}
    for field_name, field in schema.fields.items():
        field_key = field.data_key or field_name
        field_type = field.__class__.__name__
        field_dict = {
            'type': field_type
        }
        # TODO handle list fields
        try:
            nested_schema = field.nested
            if inspect.isclass(nested_schema):
                nested_schema = nested_schema()
            if current_depth < max_depth:
                field_dict['schema'] = make_schema_dict(nested_schema, is_input=is_input, current_depth=current_depth + 1, max_depth=max_depth)
        except AttributeError:
            pass
        if is_input:
            field_dict['required'] = field.required
        if field.missing != missing:
            field_dict['missing'] = field.missing
        if field.default != missing:
            field_dict['default'] = field.default
        field_dict.update(field.metadata)
        schema_dict[field_key] = field_dict
    return schema_dict

def set_unknown_all(schema):
    schema.unknown = EXCLUDE
    for field in schema.fields.values():
            if isinstance(field, fields.Nested):
                set_unknown_all(field.schema)

    return schema

def use_input_schema(schema):
    if inspect.isclass(schema):
        schema = schema()

    def view_decorator(func):
        func._uses_smores = True
        func._input_schema = schema
        found_ins = {}
        for field_name, field in schema.fields.items():
            if field.metadata.get('found_in') in {'path', 'query', 'header', 'cookie', 'json'}:
                found_ins[field_name] = field.metadata['found_in']

        @wraps(func)
        def decorated_view(*args, **kwargs):
            data = CaseInsensitiveDict()
            data.update(request.headers)
            data.update(request.cookies)
            data.update(request.args)
            data.update(request.view_args)
            if request.method in {'POST', 'PUT', 'PATCH'}:
                json = request.get_json(force=True)
                if type(json) != dict:
                    json = {
                        'json_body': json
                    }
            else:
                json = {}

            try:
                data.update(json)
            except TypeError:
                pass

            for field_name, found_in in found_ins.items():
                # TODO: make more efficient. right now it sets these keys twice
                if found_in == 'json':
                    try:
                        value = json.get(field_name)
                        if value is not None:
                            data[field_name] = value
                    except AttributeError:
                        pass
                else:
                    try:
                        value = getattr(request, REQUEST_ATTR_MAP[found_in]).get(field_name)
                        if value is not None:
                            data[field_name] = value
                    except AttributeError:
                        pass

            set_unknown_all(schema)

            try:
                data = schema.load(data=data)
            except ValidationError:
                errors = schema.validate(data)
                response_data = {
                    'errors': errors
                }
                route_docs = current_app._api_docs.get(request.url_rule.rule, {}).get(request.method)
                if route_docs:
                    response_data['route_docs'] = route_docs
                return jsonify(response_data), 400

            request.input_obj = data
            view_args = inspect.getargspec(func).args
            if 'input_obj' in view_args:
                return func(input_obj=data, *args, **kwargs)
            else:
                return func(*args, **kwargs)

        return decorated_view

    return view_decorator


def use_output_schema(schema):
    if inspect.isclass(schema):
        schema = schema()

    def view_decorator(func):
        func._uses_smores = True
        func._output_schema = schema
        return func
    return view_decorator
