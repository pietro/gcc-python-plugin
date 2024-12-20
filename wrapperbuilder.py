#   Copyright 2012 David Malcolm <dmalcolm@redhat.com>
#   Copyright 2012 Red Hat, Inc.
#
#   This is free software: you can redistribute it and/or modify it
#   under the terms of the GNU General Public License as published by
#   the Free Software Foundation, either version 3 of the License, or
#   (at your option) any later version.
#
#   This program is distributed in the hope that it will be useful, but
#   WITHOUT ANY WARRANTY; without even the implied warranty of
#   MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the GNU
#   General Public License for more details.
#
#   You should have received a copy of the GNU General Public License
#   along with this program.  If not, see
#   <http://www.gnu.org/licenses/>.

# Shared code for gcc-python-plugin's generate-*-c.py, where the code
# is specific to gcc-python-plugin

from cpybuilder import (
    PyTypeObject,
    PyAsyncMethod,
    PyNumberMethods,
    PyMappingMethods,
    PySequenceMethods,
    PyBufferProcs,
)


def indent(lines):
    return "\n".join("    %s" % line for line in lines.splitlines())


class PyGccWrapperTypeObject(PyTypeObject):
    """
    A PyTypeObject that's also a PyGccWrapperTypeObject
    (with metaclass PyGccWrapperMeta_TypeObj)
    """

    def __init__(self, *args, **kwargs):
        PyTypeObject.__init__(self, *args, **kwargs)
        self.ob_type = "&PyGccWrapperMeta_TypeObj"

    def c_defn(self):
        result = "\n"
        result += "PyGccWrapperTypeObject %(identifier)s = {\n" % self.__dict__

        ht_type = "{\n        .ht_type = {\n%s\n" % indent(indent(self.c_initializer()))
        ht_type += "%s\n" % indent("},")

        as_async = "%s\n" % indent(".as_async = {\n")
        as_async += "%s\n" % indent(
            PyAsyncMethod("", None, None, None, None).c_initializer()
        )
        as_async += "%s\n" % indent("},")

        as_number = "%s\n" % indent(".as_number = {\n")
        as_number += "%s\n" % indent(PyNumberMethods("").c_initializer())
        as_number += "%s\n" % indent("},")

        as_mapping = "%s\n" % indent(".as_mapping = {\n")
        as_mapping += "%s\n" % indent(PyMappingMethods("").c_initializer())
        as_mapping += "%s\n" % indent("},")

        as_sequence = "%s\n" % indent(".as_sequence = {\n")
        as_sequence += "%s\n" % indent(PySequenceMethods("").c_initializer())
        as_sequence += "%s\n" % indent("},")

        as_buffer = "%s\n" % indent(".as_buffer = {\n")
        as_buffer += "%s\n" % indent(PyBufferProcs("").c_initializer())
        as_buffer += "%s\n" % indent("},")

        # as_buffer = '%s\n' % indent('.as_buffer = NULL,\n')

        ht_name = "%s\n" % indent(".ht_name = NULL,\n")
        ht_slots = "%s\n" % indent(".ht_slots = NULL,\n")
        ht_qualname = "%s\n" % indent(".ht_qualname = NULL,\n")
        ht_cached_keys = "%s\n" % indent(".ht_cached_keys = NULL,\n")
        ht_module = "%s\n" % indent(".ht_module = NULL,\n")
        ht_tpname = "%s\n" % indent("._ht_tpname = NULL,\n")
        spec_cache = "%s" % indent("._spec_cache = NULL")

        result += self.c_src_field_value(
            "wrtp_base",
            ht_type
            + as_async
            + as_number
            + as_mapping
            + as_sequence
            + as_buffer
            + ht_name
            + ht_slots
            + ht_qualname
            + ht_cached_keys
            + ht_module
            + ht_tpname
            + spec_cache,
        )
        result += "    },\n"
        result += self.c_src_field_value(
            "wrtp_mark", "PyGcc_WrtpMarkFor%s" % self.struct_name, cast="wrtp_marker"
        )
        result += "};\n"
        result += "\n"
        return result
