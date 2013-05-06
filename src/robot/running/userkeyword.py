#  Copyright 2008-2012 Nokia Siemens Networks Oyj
#
#  Licensed under the Apache License, Version 2.0 (the "License");
#  you may not use this file except in compliance with the License.
#  You may obtain a copy of the License at
#
#      http://www.apache.org/licenses/LICENSE-2.0
#
#  Unless required by applicable law or agreed to in writing, software
#  distributed under the License is distributed on an "AS IS" BASIS,
#  WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
#  See the License for the specific language governing permissions and
#  limitations under the License.

import os
import re

from robot.common import BaseLibrary, UserErrorHandler
from robot.errors import DataError, ExecutionFailed, UserKeywordExecutionFailed
from robot.variables import is_list_var, VariableSplitter
from robot.output import LOGGER
from robot import utils

from .keywords import Keywords
from .fixture import Teardown
from .timeouts import KeywordTimeout
from .arguments import (ArgumentValidator, UserKeywordArgumentParser,
                        ArgumentResolver, ArgumentMapper)


class UserLibrary(BaseLibrary):
    supports_named_arguments = True # this attribute is for libdoc

    def __init__(self, user_keywords, path=None):
        self.name = self._get_name_for_resource_file(path)
        self.handlers = utils.NormalizedDict(ignore=['_'])
        self.embedded_arg_handlers = []
        for kw in user_keywords:
            try:
                handler = EmbeddedArgsTemplate(kw, self.name)
            except DataError, err:
                LOGGER.error("Creating user keyword '%s' failed: %s"
                             % (kw.name, unicode(err)))
                continue
            except TypeError:
                handler = UserKeywordHandler(kw, self.name)
            else:
                self.embedded_arg_handlers.append(handler)
            if handler.name in self.handlers:
                error = "Keyword '%s' defined multiple times." % handler.name
                handler = UserErrorHandler(handler.name, error)
            self.handlers[handler.name] = handler

    def _get_name_for_resource_file(self, path):
        if path is None:
            return None
        return os.path.splitext(os.path.basename(path))[0]

    def has_handler(self, name):
        if BaseLibrary.has_handler(self, name):
            return True
        for template in self.embedded_arg_handlers:
            try:
                EmbeddedArgs(name, template)
            except TypeError:
                pass
            else:
                return True
        return False

    def get_handler(self, name):
        try:
            return BaseLibrary.get_handler(self, name)
        except DataError, error:
            found = self._get_embedded_arg_handlers(name)
            if not found:
                raise error
            if len(found) == 1:
                return found[0]
            self._raise_multiple_matching_keywords_found(name, found)

    def _get_embedded_arg_handlers(self, name):
        found = []
        for template in self.embedded_arg_handlers:
            try:
                found.append(EmbeddedArgs(name, template))
            except TypeError:
                pass
        return found

    def _raise_multiple_matching_keywords_found(self, name, found):
        names = utils.seq2str([f.orig_name for f in found])
        if self.name is None:
            where = "Test case file"
        else:
            where = "Resource file '%s'" % self.name
        raise DataError("%s contains multiple keywords matching name '%s'\n"
                        "Found: %s" % (where, name, names))


class UserKeywordHandler(object):
    type = 'user'

    def __init__(self, keyword, libname):
        self.name = keyword.name
        self.keywords = Keywords(keyword.steps)
        self.return_value = keyword.return_.value
        self.teardown = keyword.teardown
        self.libname = libname
        self.doc = self._doc = keyword.doc.value
        self._timeout = keyword.timeout
        self._keyword_args = keyword.args.value

    @property
    def longname(self):
        return '%s.%s' % (self.libname, self.name) if self.libname else self.name

    @property
    def shortdoc(self):
        return self.doc.splitlines()[0] if self.doc else ''

    def init_keyword(self, varz):
        self._errors = []
        self.doc = varz.replace_meta('Documentation', self._doc, self._errors)
        self.timeout = KeywordTimeout(self._timeout.value, self._timeout.message)
        self.timeout.replace_variables(varz)

    def run(self, context, arguments):
        context.start_user_keyword(self)
        try:
            return self._run(context, arguments)
        finally:
            context.end_user_keyword()

    def _run(self, context, arguments):
        # TODO: Could arguments be parsed already in __init__?
        argspec = UserKeywordArgumentParser().parse(self.longname,
                                                    self._keyword_args)
        variables = context.get_current_vars()
        if context.dry_run:
            return self._dry_run(context, variables, argspec, arguments)
        return self._normal_run(context, variables, argspec, arguments)

    def _dry_run(self, context, variables, argspec, arguments):
        arguments = self._resolve_dry_run_args(argspec, arguments)
        error = self._execute(context, variables, argspec, arguments)
        if error:
            raise error

    def _resolve_dry_run_args(self, argspec, arguments):
        ArgumentValidator(argspec).validate_dry_run(arguments)
        required_args = argspec.minargs + len(argspec.defaults)
        missing_args = required_args - len(arguments)
        return arguments + [None] * missing_args

    def _normal_run(self, context, variables, argspec, arguments):
        arguments = self._resolve_arguments(argspec, arguments, variables)
        error = self._execute(context, variables, argspec, arguments)
        if error and not error.can_continue(context.teardown):
            raise error
        return_value = self._get_return_value(variables)
        if error:
            error.return_value = return_value
            raise error
        return return_value

    def _resolve_arguments(self, argspec, arguments, variables):
        resolver = ArgumentResolver(argspec)
        mapper = ArgumentMapper(argspec)
        positional, named = resolver.resolve(arguments, variables)
        return mapper.map(positional, named, variables)

    def _execute(self, context, variables, argspec, resolved_arguments):
        self._set_variables(argspec, resolved_arguments, variables)
        argspec.trace_log_uk_args(context.output, variables)
        self._verify_keyword_is_valid()
        self.timeout.start()
        try:
            self.keywords.run(context)
        except ExecutionFailed, error:
            pass
        else:
            error = None
        td_error = self._run_teardown(context, error)
        if error or td_error:
            return UserKeywordExecutionFailed(error, td_error)

    def _set_variables(self, argspec, arguments, variables):
        before_varargs, varargs = self._split_args_and_varargs(argspec, arguments)
        for name, value in zip(argspec.positional, before_varargs):
            variables['${%s}' % name] = value
        if argspec.varargs:
            variables['@{%s}' % argspec.varargs] = varargs

    def _split_args_and_varargs(self, argspec, args):
        if not argspec.varargs:
            return args, []
        return args[:len(argspec.positional)], args[len(argspec.positional):]

    def _run_teardown(self, context, error):
        if not self.teardown:
            return None
        teardown = Teardown(self.teardown.name, self.teardown.args)
        teardown.replace_variables(context.get_current_vars(), [])
        context.start_keyword_teardown(error)
        error = teardown.run(context)
        context.end_keyword_teardown()
        return error

    def _verify_keyword_is_valid(self):
        if self._errors:
            raise DataError('User keyword initialization failed:\n%s'
                            % '\n'.join(self._errors))
        if not (self.keywords or self.return_value):
            raise DataError("User keyword '%s' contains no keywords"
                            % self.name)

    def _get_return_value(self, variables):
        if not self.return_value:
            return None
        try:
            ret = variables.replace_list(self.return_value)
        except DataError, err:
            raise DataError('Replacing variables from keyword return value '
                            'failed: %s' % unicode(err))
        if len(ret) != 1 or is_list_var(self.return_value[0]):
            return ret
        return ret[0]


class EmbeddedArgsTemplate(UserKeywordHandler):
    _regexp_extension = re.compile(r'(?<!\\)\(\?.+\)')
    _regexp_group_start = re.compile(r'(?<!\\)\((.*?)\)')
    _regexp_group_escape = r'(?:\1)'
    _default_pattern = '.*?'
    _variable_pattern = r'\$\{[^\}]+\}'

    def __init__(self, keyword, libname):
        if keyword.args.value:
            raise TypeError('Cannot have normal arguments')
        self.embedded_args, self.name_regexp \
                = self._read_embedded_args_and_regexp(keyword.name)
        if not self.embedded_args:
            raise TypeError('Must have embedded arguments')
        UserKeywordHandler.__init__(self, keyword, libname)
        self.keyword = keyword

    def _read_embedded_args_and_regexp(self, string):
        args = []
        full_pattern = ['^']
        while True:
            before, variable, rest = self._split_from_variable(string)
            if before is None:
                break
            variable, pattern = self._get_regexp_pattern(variable)
            args.append('${%s}' % variable)
            full_pattern.extend([re.escape(before), '(%s)' % pattern])
            string = rest
        full_pattern.extend([re.escape(rest), '$'])
        return args, self._compile_regexp(full_pattern)

    def _split_from_variable(self, string):
        var = VariableSplitter(string, identifiers=['$'])
        if var.identifier is None:
            return None, None, string
        return string[:var.start], var.base, string[var.end:]

    def _get_regexp_pattern(self, variable):
        if ':' not in variable:
            return variable, self._default_pattern
        variable, pattern = variable.split(':', 1)
        return variable, self._format_custom_regexp(pattern)

    def _format_custom_regexp(self, pattern):
        for formatter in (self._regexp_extensions_are_not_allowed,
                          self._make_groups_non_capturing,
                          self._unescape_closing_curly,
                          self._add_automatic_variable_pattern):
            pattern = formatter(pattern)
        return pattern

    def _regexp_extensions_are_not_allowed(self, pattern):
        if not self._regexp_extension.search(pattern):
            return pattern
        raise DataError('Regexp extensions are not allowed in embedded '
                        'arguments.')

    def _make_groups_non_capturing(self, pattern):
        return self._regexp_group_start.sub(self._regexp_group_escape, pattern)

    def _unescape_closing_curly(self, pattern):
        return pattern.replace('\\}', '}')

    def _add_automatic_variable_pattern(self, pattern):
        return '%s|%s' % (pattern, self._variable_pattern)

    def _compile_regexp(self, pattern):
        try:
            return re.compile(''.join(pattern), re.IGNORECASE)
        except:
            raise DataError("Compiling embedded arguments regexp failed: %s"
                            % utils.get_error_message())


class EmbeddedArgs(UserKeywordHandler):

    def __init__(self, name, template):
        match = template.name_regexp.match(name)
        if not match:
            raise TypeError('Does not match given name')
        UserKeywordHandler.__init__(self, template.keyword, template.libname)
        self.embedded_args = zip(template.embedded_args, match.groups())
        self.name = name
        self.orig_name = template.name

    def _run(self, context, args):
        if not context.dry_run:
            for name, value in self.embedded_args:
                context.get_current_vars()[name] = \
                    context.get_current_vars().replace_scalar(value)
        return UserKeywordHandler._run(self, context, args)
