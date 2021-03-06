#!/usr/bin/env python

"""
Python client for the Rosette API.

Copyright (c) 2014-2015 Basis Technology Corporation.

Licensed under the Apache License, Version 2.0 (the "License");
you may not use this file except in compliance with the License.
You may obtain a copy of the License at
http://www.apache.org/licenses/LICENSE-2.0

Unless required by applicable law or agreed to in writing, software
distributed under the License is distributed on an "AS IS" BASIS,
WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
See the License for the specific language governing permissions and
limitations under the License.
"""

from io import BytesIO
import gzip
import json
import logging
import sys
import time
import os
from socket import gaierror
import requests

_BINDING_VERSION = "1.1"
_GZIP_BYTEARRAY = bytearray([0x1F, 0x8b, 0x08])

_IsPy3 = sys.version_info[0] == 3


try:
    import urlparse
except ImportError:
    import urllib.parse as urlparse
try:
    import httplib
except ImportError:
    import http.client as httplib

if _IsPy3:
    _GZIP_SIGNATURE = _GZIP_BYTEARRAY
else:
    _GZIP_SIGNATURE = str(_GZIP_BYTEARRAY)


class _ReturnObject:

    def __init__(self, js, code):
        self._json = js
        self.status_code = code

    def json(self):
        return self._json


def _my_loads(obj, response_headers):
    if _IsPy3:
        d1 = json.loads(obj.decode("utf-8")).copy()
        d1.update(response_headers)
        return d1  # if py3, need chars.
    else:
        d2 = json.loads(obj).copy()
        d2.update(response_headers)
        return d2


class RosetteException(Exception):
    """Exception thrown by all Rosette API operations for errors local and remote.

    TBD. Right now, the only valid operation is conversion to __str__.
    """

    def __init__(self, status, message, response_message):
        self.status = status
        self.message = message
        self.response_message = response_message

    def __str__(self):
        sst = self.status
        if not (isinstance(sst, str)):
            sst = repr(sst)
        return sst + ": " + self.message + ":\n  " + self.response_message


class _PseudoEnum:

    def __init__(self):
        pass

    @classmethod
    def validate(cls, value, name):
        values = []
        for (k, v) in vars(cls).items():
            if not k.startswith("__"):
                values += [v]

        # this is still needed to make sure that the parameter NAMES are known.
        # If python didn't allow setting unknown values, this would be a
        # language error.
        if value not in values:
            raise RosetteException(
                "unknownVariable",
                "The value supplied for " +
                name +
                " is not one of " +
                ", ".join(values) +
                ".",
                repr(value))


class MorphologyOutput(_PseudoEnum):
    LEMMAS = "lemmas"
    PARTS_OF_SPEECH = "parts-of-speech"
    COMPOUND_COMPONENTS = "compound-components"
    HAN_READINGS = "han-readings"
    COMPLETE = "complete"


class _DocumentParamSetBase(object):

    def __init__(self, repertoire):
        self.__params = {}
        for k in repertoire:
            self.__params[k] = None

    def __setitem__(self, key, val):
        if key not in self.__params:
            raise RosetteException(
                "badKey", "Unknown Rosette parameter key", repr(key))
        self.__params[key] = val

    def __getitem__(self, key):
        if key not in self.__params:
            raise RosetteException(
                "badKey", "Unknown Rosette parameter key", repr(key))
        return self.__params[key]

    def validate(self):
        pass

    def serialize(self):
        self.validate()
        v = {}
        for (key, val) in self.__params.items():
            if val is None:
                pass
            else:
                v[key] = val
        return v


def _byteify(s):  # py 3 only
    l = len(s)
    b = bytearray(l)
    for ix in range(l):
        oc = ord(s[ix])
        assert (oc < 256)
        b[ix] = oc
    return b


class DocumentParameters(_DocumentParamSetBase):
    """Parameter object for all operations requiring input other than
    translated_name.
    Two fields, C{content} and C{inputUri}, are set via
    the subscript operator, e.g., C{params["content"]}, or the
    convenience instance methods L{DocumentParameters.load_document_file}
    and L{DocumentParameters.load_document_string}.

    Using subscripts instead of instance variables facilitates diagnosis.

    If the field C{contentUri} is set to the URL of a web page (only
    protocols C{http, https, ftp, ftps} are accepted), the server will
    fetch the content from that web page.  In this case, C{content} may not be set.
    """

    def __init__(self):
        """Create a L{DocumentParameters} object."""
        _DocumentParamSetBase.__init__(
            self, ("content", "contentUri", "language", "genre"))
        self.file_name = ""
        self.useMultipart = False

    def validate(self):
        """Internal. Do not use."""
        if self["content"] is None:
            if self["contentUri"] is None:
                raise RosetteException(
                    "badArgument",
                    "Must supply one of Content or ContentUri",
                    "bad arguments")
        else:  # self["content"] not None
            if self["contentUri"] is not None:
                raise RosetteException(
                    "badArgument",
                    "Cannot supply both Content and ContentUri",
                    "bad arguments")

    def serialize(self):
        """Internal. Do not use."""
        self.validate()
        slz = super(DocumentParameters, self).serialize()
        return slz

    def load_document_file(self, path):
        """Loads a file into the object.
        The file will be read as bytes; the appropriate conversion will
        be determined by the server.
        @parameter path: Pathname of a file acceptable to the C{open} function.
        """
        self.useMultipart = True
        self.file_name = path
        self.load_document_string(open(path, "rb").read())

    def load_document_string(self, s):
        """Loads a string into the object.
        The string will be taken as bytes or as Unicode dependent upon
        its native python type.
        @parameter s: A string, possibly a unicode-string, to be loaded
        for subsequent analysis.
        """
        self["content"] = s


class RelationshipsParameters(DocumentParameters):

    """Parameter object for relationships endpoint. Inherits from L(DocumentParameters), but allows the user
    to specify the relationships-unique options parameter."""

    def __init__(self):
        """Create a L{RelationshipsParameters} object."""
        self.useMultipart = False
        _DocumentParamSetBase.__init__(
            self, ("content", "contentUri", "language", "options", "genre"))


class NameTranslationParameters(_DocumentParamSetBase):
    """Parameter object for C{name-translation} endpoint.
    The following values may be set by the indexing (i.e.,C{ parms["name"]}) operator.  The values are all
    strings (when not C{None}).
    All are optional except C{name} and C{targetLanguage}.  Scripts are in
    ISO15924 codes, and languages in ISO639 (two- or three-letter) codes.  See the Name Translation documentation for
    more description of these terms, as well as the content of the return result.

    C{name} The name to be translated.

    C{targetLangauge} The language into which the name is to be translated.

    C{entityType} The entity type (TBD) of the name.

    C{sourceLanguageOfOrigin} The language of origin of the name.

    C{sourceLanguageOfUse} The language of use of the name.

    C{sourceScript} The script in which the name is supplied.

    C{targetScript} The script into which the name should be translated.

    C{targetScheme} The transliteration scheme by which the translated name should be rendered.
    """

    def __init__(self):
        self.useMultipart = False
        _DocumentParamSetBase.__init__(
            self,
            ("name",
             "targetLanguage",
             "entityType",
             "sourceLanguageOfOrigin",
             "sourceLanguageOfUse",
             "sourceScript",
             "targetScript",
             "targetScheme",
             "genre"))

    def validate(self):
        """Internal. Do not use."""
        for n in ("name", "targetLanguage"):  # required
            if self[n] is None:
                raise RosetteException(
                    "missingParameter",
                    "Required Name Translation parameter not supplied",
                    repr(n))


class NameSimilarityParameters(_DocumentParamSetBase):
    """Parameter object for C{name-similarity} endpoint.
    All are required.

    C{name1} The name to be matched, a C{name} object.

    C{name2} The name to be matched, a C{name} object.

    The C{name} object contains these fields:

    C{text} Text of the name, required.

    C{language} Language of the name in ISO639 three-letter code, optional.

    C{script} The ISO15924 code of the name, optional.

    C{entityType} The entity type, can be "PERSON", "LOCATION" or "ORGANIZATION", optional.
    """

    def __init__(self):
        self.useMultipart = False
        _DocumentParamSetBase.__init__(self, ("name1", "name2"))

    def validate(self):
        """Internal. Do not use."""
        for n in ("name1", "name2"):  # required
            if self[n] is None:
                raise RosetteException(
                    "missingParameter",
                    "Required Name Similarity parameter not supplied",
                    repr(n))


class EndpointCaller:
    """L{EndpointCaller} objects are invoked via their instance methods to obtain results
    from the Rosette server described by the L{API} object from which they
    are created.  Each L{EndpointCaller} object communicates with a specific endpoint
    of the Rosette server, specified at its creation.  Use the specific
    instance methods of the L{API} object to create L{EndpointCaller} objects bound to
    corresponding endpoints.

    Use L{EndpointCaller.ping} to ping, and L{EndpointCaller.info} to retrieve server info.
    For all other types of requests, use L{EndpointCaller.call}, which accepts
    an argument specifying the data to be processed and certain metadata.

    The results of all operations are returned as python dictionaries, whose
    keys and values correspond exactly to those of the corresponding
    JSON return value described in the Rosette web service documentation.
    """

    def __init__(self, api, suburl):
        """This method should not be invoked by the user.  Creation is reserved
        for internal use by API objects."""

        self.service_url = api.service_url
        self.user_key = api.user_key
        self.logger = api.logger
        self.useMultipart = False
        self.suburl = suburl
        self.debug = api.debug
        self.api = api

    def __finish_result(self, r, ename):
        code = r.status_code
        the_json = r.json()
        if code == 200:
            return the_json
        else:
            if 'message' in the_json:
                msg = the_json['message']
            else:
                msg = the_json['code']  # punt if can't get real message
            if self.suburl is None:
                complaint_url = "Top level info"
            else:
                complaint_url = ename + " " + self.suburl

            raise RosetteException(code, complaint_url +
                                   " : failed to communicate with Rosette", msg)

    def info(self):
        """Issues an "info" request to the L{EndpointCaller}'s specific endpoint.
        @return: A dictionary telling server version and other
        identifying data."""
        url = self.service_url + "info"
        headers = {'Accept': 'application/json', 'X-RosetteAPI-Binding': 'python', 'X-RosetteAPI-Binding-Version': _BINDING_VERSION}
        if self.debug:
            headers['X-RosetteAPI-Devel'] = 'true'
        self.logger.info('info: ' + url)
        if self.user_key is not None:
            headers["X-RosetteAPI-Key"] = self.user_key
        r = self.api._get_http(url, headers=headers)
        return self.__finish_result(r, "info")

    def ping(self):
        """Issues a "ping" request to the L{EndpointCaller}'s (server-wide) endpoint.
        @return: A dictionary if OK.  If the server cannot be reached,
        or is not the right server or some other error occurs, it will be
        signalled."""

        url = self.service_url + 'ping'
        headers = {'Accept': 'application/json', 'X-RosetteAPI-Binding': 'python', 'X-RosetteAPI-Binding-Version': _BINDING_VERSION}
        if self.debug:
            headers['X-RosetteAPI-Devel'] = 'true'
        self.logger.info('Ping: ' + url)
        if self.user_key is not None:
            headers["X-RosetteAPI-Key"] = self.user_key
        r = self.api._get_http(url, headers=headers)
        return self.__finish_result(r, "ping")

    def call(self, parameters):
        """Invokes the endpoint to which this L{EndpointCaller} is bound.
        Passes data and metadata specified by C{parameters} to the server
        endpoint to which this L{EndpointCaller} object is bound.  For all
        endpoints except C{name-translation} and C{name-similarity}, it must be a L{DocumentParameters}
        object or a string; for C{name-translation}, it must be an L{NameTranslationParameters} object;
        for C{name-similarity}, it must be an L{NameSimilarityParameters} object. For relationships,
        it may be an L(DocumentParameters) or an L(RelationshipsParameters).

        In all cases, the result is returned as a python dictionary
        conforming to the JSON object described in the endpoint's entry
        in the Rosette web service documentation.

        @param parameters: An object specifying the data,
        and possible metadata, to be processed by the endpoint.  See the
        details for those object types.
        @type parameters: For C{name-translation}, L{NameTranslationParameters}, otherwise L{DocumentParameters} or L{str}
        @return: A python dictionary expressing the result of the invocation.
        """

        if not isinstance(parameters, _DocumentParamSetBase):
            if self.suburl != "name-similarity" and self.suburl != "name-translation":
                text = parameters
                parameters = DocumentParameters()
                parameters['content'] = text
            else:
                raise RosetteException(
                    "incompatible",
                    "Text-only input only works for DocumentParameter endpoints",
                    self.suburl)

        self.useMultipart = parameters.useMultipart
        url = self.service_url + self.suburl
        params_to_serialize = parameters.serialize()
        headers = {}
        if self.user_key is not None:
            headers["X-RosetteAPI-Key"] = self.user_key
            headers["X-RosetteAPI-Binding"] = "python"
            headers["X-RosetteAPI-Binding-Version"] = _BINDING_VERSION
        if self.useMultipart:
            params = dict(
                (key,
                 value) for key,
                value in params_to_serialize.iteritems() if key == 'language')
            files = {
                'content': (
                    os.path.basename(
                        parameters.file_name),
                    params_to_serialize["content"],
                    'text/plain'),
                'request': (
                    'request_options',
                    json.dumps(params),
                    'application/json')}
            request = requests.Request(
                'POST', url, files=files, headers=headers)
            prepared_request = request.prepare()
            session = requests.Session()
            resp = session.send(prepared_request)
            rdata = resp.content
            response_headers = {"responseHeaders": dict(resp.headers)}
            status = resp.status_code
            r = _ReturnObject(_my_loads(rdata, response_headers), status)
        else:
            if self.debug:
                headers['X-RosetteAPI-Devel'] = True
            self.logger.info('operate: ' + url)
            headers['Accept'] = "application/json"
            headers['Accept-Encoding'] = "gzip"
            headers['Content-Type'] = "application/json"
            r = self.api._post_http(url, params_to_serialize, headers)
        return self.__finish_result(r, "operate")


class API:
    """
    Rosette Python Client Binding API; representation of a Rosette server.
    Call instance methods upon this object to obtain L{EndpointCaller} objects
    which can communicate with particular Rosette server endpoints.
    """

    def __init__(
            self,
            user_key=None,
            service_url='https://api.rosette.com/rest/v1/',
            retries=5,
            reuse_connection=True,
            refresh_duration=0.5,
            debug=False):
        """ Create an L{API} object.
        @param user_key: (Optional; required for servers requiring authentication.) An authentication string to be sent
         as user_key with all requests.  The default Rosette server requires authentication.
         to the server.
        """
        # logging.basicConfig(filename="binding.log", filemode="w", level=logging.DEBUG)
        self.user_key = user_key
        self.service_url = service_url if service_url.endswith(
            '/') else service_url + '/'
        self.logger = logging.getLogger('rosette.api')
        self.logger.info('Initialized on ' + self.service_url)
        self.debug = debug

        if (retries < 1):
            retries = 1
        if (refresh_duration < 0):
            refresh_duration = 0

        self.num_retries = retries
        self.reuse_connection = reuse_connection
        self.connection_refresh_duration = refresh_duration
        self.http_connection = None

    def _connect(self, parsedUrl):
        """ Simple connection method
        @param parsedUrl: The URL on which to process
        """
        if not self.reuse_connection or self.http_connection is None:
            loc = parsedUrl.netloc
            if parsedUrl.scheme == "https":
                self.http_connection = httplib.HTTPSConnection(loc)
            else:
                self.http_connection = httplib.HTTPConnection(loc)

    def _make_request(self, op, url, data, headers):
        """
        Handles the actual request, retrying if a 429 is encountered

        @param op: POST or GET
        @param url: endpoing URL
        @param data: request data
        @param headers: request headers
        """
        headers['User-Agent'] = "RosetteAPIPython/" + _BINDING_VERSION
        parsedUrl = urlparse.urlparse(url)

        self._connect(parsedUrl)

        message = None
        code = "unknownError"
        rdata = None
        response_headers = {}
        for i in range(self.num_retries + 1):
            try:
                self.http_connection.request(op, url, data, headers)
                response = self.http_connection.getresponse()
                status = response.status
                rdata = response.read()
                response_headers["responseHeaders"] = (
                    dict(response.getheaders()))
                if status == 200:
                    if not self.reuse_connection:
                        self.http_connection.close()
                    return rdata, status, response_headers
                if status == 429:
                    code = status
                    message = "{0} ({1})".format(rdata, i)
                    time.sleep(self.connection_refresh_duration)
                    self.http_connection.close()
                    self._connect(parsedUrl)
                    continue
                if rdata is not None:
                    try:
                        the_json = _my_loads(rdata, response_headers)
                        if 'message' in the_json:
                            message = the_json['message']
                        if "code" in the_json:
                            code = the_json['code']
                        else:
                            code = status
                        raise RosetteException(code, message, url)
                    except:
                        raise
            except (httplib.BadStatusLine, gaierror):
                raise RosetteException(
                    "ConnectionError",
                    "Unable to establish connection to the Rosette API server",
                    url)

        if not self.reuse_connection:
            self.http_connection.close()

        raise RosetteException(code, message, url)

    def _get_http(self, url, headers):
        """
        Simple wrapper for the GET request

        @param url: endpoint URL
        @param headers: request headers
        """
        (rdata, status, response_headers) = self._make_request(
            "GET", url, None, headers)
        return _ReturnObject(_my_loads(rdata, response_headers), status)

    def _post_http(self, url, data, headers):
        """
        Simple wrapper for the POST request

        @param url: endpoint URL
        @param data: request data
        @param headers: request headers
        """
        if data is None:
            json_data = ""
        else:
            json_data = json.dumps(data)

        (rdata, status, response_headers) = self._make_request(
            "POST", url, json_data, headers)

        if len(rdata) > 3 and rdata[0:3] == _GZIP_SIGNATURE:
            buf = BytesIO(rdata)
            rdata = gzip.GzipFile(fileobj=buf).read()

        return _ReturnObject(_my_loads(rdata, response_headers), status)

    def ping(self):
        """
        Create a ping L{EndpointCaller} for the server and ping it.
        @return: A python dictionary including the ping message of the L{API}
        """
        return EndpointCaller(self, None).ping()

    def info(self):
        """
        Create a ping L{EndpointCaller} for the server and ping it.
        @return: A python dictionary including the ping message of the L{API}
        """
        return EndpointCaller(self, None).info()

    def language(self, parameters):
        """
        Create an L{EndpointCaller} for language identification and call it.
        @param parameters: An object specifying the data,
        and possible metadata, to be processed by the language identifier.
        @type parameters: L{DocumentParameters} or L{str}
        @return: A python dictionary containing the results of language
        identification."""
        return EndpointCaller(self, "language").call(parameters)

    def sentences(self, parameters):
        """
        Create an L{EndpointCaller} to break a text into sentences and call it.
        @param parameters: An object specifying the data,
        and possible metadata, to be processed by the sentence identifier.
        @type parameters: L{DocumentParameters} or L{str}
        @return: A python dictionary containing the results of sentence identification."""
        return EndpointCaller(self, "sentences").call(parameters)

    def tokens(self, parameters):
        """
        Create an L{EndpointCaller} to break a text into tokens and call it.
        @param parameters: An object specifying the data,
        and possible metadata, to be processed by the tokens identifier.
        @type parameters: L{DocumentParameters} or L{str}
        @return: A python dictionary containing the results of tokenization."""
        return EndpointCaller(self, "tokens").call(parameters)

    def morphology(self, parameters, facet=MorphologyOutput.COMPLETE):
        """
        Create an L{EndpointCaller} to returns a specific facet
        of the morphological analyses of texts to which it is applied and call it.
        @param parameters: An object specifying the data,
        and possible metadata, to be processed by the morphology analyzer.
        @type parameters: L{DocumentParameters} or L{str}
        @param facet: The facet desired, to be returned by the created L{EndpointCaller}.
        @type facet: An element of L{MorphologyOutput}.
        @return: A python dictionary containing the results of morphological analysis."""
        return EndpointCaller(self, "morphology/" + facet).call(parameters)

    def entities(self, parameters, resolve_entities=False):
        """
        Create an L{EndpointCaller}  to identify named entities found in the texts
        to which it is applied and call it. Linked entity information is optional, and
        its need must be specified at the time the operator is created.
        @param parameters: An object specifying the data,
        and possible metadata, to be processed by the entity identifier.
        @type parameters: L{DocumentParameters} or L{str}
        @param resolve_entities: Specifies whether or not linked entity information will
        be wanted.
        @type resolve_entities: Boolean
        @return: A python dictionary containing the results of entity extraction."""
        if resolve_entities:
            return EndpointCaller(self, "entities/linked").call(parameters)
        else:
            return EndpointCaller(self, "entities").call(parameters)

    def categories(self, parameters):
        """
        Create an L{EndpointCaller} to identify the category of the text to which
        it is applied and call it.
        @param parameters: An object specifying the data,
        and possible metadata, to be processed by the category identifier.
        @type parameters: L{DocumentParameters} or L{str}
        @return: A python dictionary containing the results of categorization."""
        return EndpointCaller(self, "categories").call(parameters)

    def sentiment(self, parameters):
        """
        Create an L{EndpointCaller} to identify the sentiment of the text to
        which it is applied and call it.
        @param parameters: An object specifying the data,
        and possible metadata, to be processed by the sentiment identifier.
        @type parameters: L{DocumentParameters} or L{str}
        @return: A python dictionary containing the results of sentiment identification."""
        """Create an L{EndpointCaller} to identify sentiments of the texts
        to which is applied.
        @return: An L{EndpointCaller} object which can return sentiments
        of texts to which it is applied."""
        return EndpointCaller(self, "sentiment").call(parameters)

    def relationships(self, parameters):
        """
        Create an L{EndpointCaller} to identify the relationships between entities in the text to
        which it is applied and call it.
        @param parameters: An object specifying the data,
        and possible metadata, to be processed by the relationships identifier.
        @type parameters: L{DocumentParameters}, L(RelationshipsParameters), or L{str}
        @return: A python dictionary containing the results of relationship extraction."""
        return EndpointCaller(self, "relationships").call(parameters)

    def name_translation(self, parameters):
        """
        Create an L{EndpointCaller} to perform name analysis and translation
        upon the name to which it is applied and call it.
        @param parameters: An object specifying the data,
        and possible metadata, to be processed by the name translator.
        @type parameters: L{NameTranslationParameters}
        @return: A python dictionary containing the results of name translation."""
        return EndpointCaller(self, "name-translation").call(parameters)

    def translated_name(self, parameters):
        """ deprecated
        Call name_translation to perform name analysis and translation
        upon the name to which it is applied.
        @param parameters: An object specifying the data,
        and possible metadata, to be processed by the name translator.
        @type parameters: L{NameTranslationParameters}
        @return: A python dictionary containing the results of name translation."""
        return self.name_translation(parameters)

    def name_similarity(self, parameters):
        """
        Create an L{EndpointCaller} to perform name similarity scoring and call it.
        @param parameters: An object specifying the data,
        and possible metadata, to be processed by the name matcher.
        @type parameters: L{NameSimilarityParameters}
        @return: A python dictionary containing the results of name matching."""
        return EndpointCaller(self, "name-similarity").call(parameters)

    def matched_name(self, parameters):
        """ deprecated
        Call name_similarity to perform name matching.
        @param parameters: An object specifying the data,
        and possible metadata, to be processed by the name matcher.
        @type parameters: L{NameSimilarityParameters}
        @return: A python dictionary containing the results of name matching."""
        return self.name_similarity(parameters)
