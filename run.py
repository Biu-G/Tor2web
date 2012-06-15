"""
    Tor2web
    Copyright (C) 2012 Hermes No Profit Association - GlobaLeaks Project

    This program is free software: you can redistribute it and/or modify
    it under the terms of the GNU Affero General Public License as published by
    the Free Software Foundation, either version 3 of the License, or
    (at your option) any later version.

    This program is distributed in the hope that it will be useful,
    but WITHOUT ANY WARRANTY; without even the implied warranty of
    MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
    GNU Affero General Public License for more details.

    You should have received a copy of the GNU Affero General Public License
    along with this program.  If not, see <http://www.gnu.org/licenses/>.
"""

"""

:mod:`Tor2Web`
=====================================================

.. automodule:: Tor2Web
   :synopsis: [GLOBALEAKS_MODULE_DESCRIPTION]

.. moduleauthor:: Arturo Filasto' <art@globaleaks.org>
.. moduleauthor:: Giovanni Pellerano <evilaliv3@globaleaks.org>

"""
# -*- coding: utf-8 -*-

from twisted.application import service, internet
from twisted.internet import ssl, reactor, endpoints
from twisted.python import log
from twisted.web import proxy, http, client, server, static, resource
from twisted.web.http import Request

from socksclient import SOCKSv4ClientProtocol, SOCKSWrapper
from OpenSSL import SSL

import hashlib
import gzip
import os
import sys
import urlparse
import re

from StringIO import StringIO
from mimetypes import guess_type

from config import Config
from tor2web import Tor2web
from storage import Storage

def startTor2webHTTP(t2w):
    return internet.TCPServer(int(t2w.config.listen_port_http), T2WProxyFactory())

def startTor2webHTTPS(t2w):
    return internet.SSLServer(int(t2w.config.listen_port_https), T2WProxyFactory(), T2WSSLContextFactory(t2w.config.sslkeyfile, t2w.config.sslcertfile, t2w.config.ssldhfile, t2w.config.cipher_list))

config = Config("main")

t2w = Tor2web(config)

application = service.Application("Tor2web")

class T2WSSLContextFactory():
    """
    """
    _context = None

    def __init__(self, privateKeyFileName, certificateFileName, dhFileName, cipherList):
        """
        @param privateKeyFileName: Name of a file containing a private key
        @param certificateFileName: Name of a file containing a certificate
        @param cipherList: The SSL cipher list selection to use
        """
        if self._context is None:
            ctx = SSL.Context(SSL.SSLv23_METHOD)
            # Disallow SSLv2!  It's insecure!
            ctx.set_options(SSL.OP_NO_SSLv2)
            ctx.use_certificate_file(certificateFileName)
            ctx.use_privatekey_file(privateKeyFileName)
            ctx.set_cipher_list(cipherList)
            ctx.load_tmp_dh(dhFileName)
            self._context = ctx

    def __getstate__(self):
        d = self.__dict__.copy()
        del d['_context']
        return d

    def __setstate__(self, state):
        self.__dict__ = state

    def getContext(self):
        """
        Return an SSL context.
        """
        return self._context

class T2WProxyClient(proxy.ProxyClient):
    def __init__(self, *args, **kwargs):
        proxy.ProxyClient.__init__(self, *args, **kwargs)
        self.bf = []
        self.contenttype = 'unknown'
        self.gzip = False
        self.html = False
        self.location = False
        self._chunked = False

    def handleHeader(self, key, value):
        keyLower = key.lower()

        if keyLower == "content-encoding" and value == "gzip":
            self.decoder = GzipIncrementalDecoder
            return
      
        if keyLower == "location":
            self.location = t2w.fix_link(value)
            return

        if keyLower == "transfer-encoding" and value == "chunked":
            self._chunked = http._ChunkedTransferDecoder(self.handleResponsePart,
                                                         self.handleResponseEnd)
            return

        if keyLower == 'content-type' and re.search('text/html', value):
            self.html = True

        if keyLower == "content-length":
            pass

        elif keyLower == 'cache-control':
            pass

        elif keyLower == 'connection':
            pass

        else:
            proxy.ProxyClient.handleHeader(self, key, value)

    def handleEndHeaders(self):
        if self.location:
            proxy.ProxyClient.handleHeader(self, "location", self.location)

    def rawDataReceived(self, data):
        if self.length is not None:
            data, rest = data[:self.length], data[self.length:]
            self.length -= len(data)
        else:
            rest = ''

        if self._chunked:
            self._chunked.dataReceived(data)
        else:
            self.handleResponsePart(data)

        if self.length == 0:
            self.handleResponseEnd()
            self.setLineMode(rest)

    def handleResponsePart(self, buffer):
        self.bf.append(buffer)

    def connectionLost(self, reason):
        proxy.ProxyClient.handleResponseEnd(self)

    def handleResponseEnd(self, *args, **kwargs):
        content = ''.join(self.bf)

        if self.gzip:
            c_f = StringIO(content)
            content = gzip.GzipFile(fileobj=c_f).read()

        if self.html:
            content = t2w.process_html(content)

        if content:
            proxy.ProxyClient.handleHeader(self, 'cache-control', 'no-cache')
            proxy.ProxyClient.handleHeader(self, "content-length", len(content))
            proxy.ProxyClient.handleEndHeaders(self)
            proxy.ProxyClient.handleResponsePart(self, content)

        proxy.ProxyClient.handleResponseEnd(self)
        self.finish()
        return server.NOT_DONE_YET

    def finish(self):
        pass

class T2WProxyClientFactory(proxy.ProxyClientFactory):
    protocol = T2WProxyClient

class T2WRequest(Request):
    """
    Used by Tor2webProxy to implement a simple web proxy.
    """
    protocols = {'http': T2WProxyClientFactory}
    ports = {'http': 80}

    def process(self):
        myrequest = Storage()
        myrequest.headers = self.getAllHeaders().copy()
        myrequest.uri = self.uri
        myrequest.host = myrequest.headers['host']

        if self.uri == "/robots.txt" and config.blockcrawl:
            self.write("User-Agent: *\nDisallow: /\n")
            self.finish()
            return server.NOT_DONE_YET

        if myrequest.headers['user-agent'] in t2w.blocked_ua:
            # Detected a blocked user-agent
            # Setting response code to 410 and sending Blocked UA string
            self.setResponseCode(410)
            self.write("Blocked UA\n")
            self.finish()
            return server.NOT_DONE_YET

        if self.uri.lower().endswith(('gif','jpg','png')):
            if not 'referer' in myrequest.headers or not config.basehost in myrequest.headers['referer'].lower():
                self.write(open('static/tor2web-small.png', 'r').read())
                self.finish()
                return server.NOT_DONE_YET

        if not t2w.process_request(myrequest):
            self.setResponseCode(t2w.error['code'])
            self.write(t2w.error['message'])
            log.msg(str(t2w.error['code']) + ' ' + t2w.error['message'])
            self.finish()
            return server.NOT_DONE_YET

        # Rewrite the URI with the tor2web parsed one
        self.uri = t2w.address

        parsed = urlparse.urlparse(self.uri)
        protocol = parsed[0]
        host = parsed[1]
        if ':' in host:
            host, port = host.split(':')
            port = int(port)
        else:
            port = self.ports[protocol]

        rest = urlparse.urlunparse(('', '') + parsed[2:])
        if not rest:
            rest = rest + '/'

        class_ = self.protocols[protocol]
        headers = self.getAllHeaders().copy()

        self.content.seek(0, 0)

        dest = client._parse(t2w.address) # scheme, host, port, path
        proxy = (None, 'localhost', 9050, True, None, None)
        endpoint = endpoints.TCP4ClientEndpoint(reactor, dest[1], dest[2])
        wrapper = SOCKSWrapper(reactor, proxy[1], proxy[2], endpoint)
        f = class_(self.method, rest, self.clientproto, headers, self.content.read(), self)
        d = wrapper.connect(f)

        return server.NOT_DONE_YET

class T2WProxy(proxy.Proxy):
      requestFactory = T2WRequest

class T2WProxyFactory(http.HTTPFactory):
    protocol = T2WProxy

    def __init__(self):
        """Initialize.
        """
        http.HTTPFactory.__init__(self, logPath=config.logpath)
        self.sessions = {}
        self.resource = resource
      
    def log(self, request):
      """
      Log a request's result to the logfile, by default in combined log format.
      """
      if config.logreqs and hasattr(self, "logFile"):
        line = '127.0.0.1 - - %s "%s" %d %s "%s" "%s"\n' % (
          self._logDateTime,
          '%s %s %s' % (self._escape(request.method),
                        self._escape(request.uri),
                        self._escape(request.clientproto)),
          request.code,
          request.sentLength or "-",
          self._escape(request.getHeader("referer") or "-"),
          self._escape(request.getHeader("user-agent") or "-"))
        self.logFile.write(line)


service_https = startTor2webHTTPS(t2w)
service_https.setServiceParent(application)

service_http = startTor2webHTTP(t2w)
service_http.setServiceParent(application)
