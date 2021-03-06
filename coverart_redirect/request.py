#!/usr/bin/env python2

# Copyright (C) 2011 Lukas Lalinsky
# Copyright (C) 2011 Robert Kaye
# Copyright (C) 2012 MetaBrainz Foundation Inc.
#
# Permission is hereby granted, free of charge, to any person obtaining a copy
# of this software and associated documentation files (the "Software"), to deal
# in the Software without restriction, including without limitation the rights
# to use, copy, modify, merge, publish, distribute, sublicense, and/or sell
# copies of the Software, and to permit persons to whom the Software is
# furnished to do so, subject to the following conditions:
#
# The above copyright notice and this permission notice shall be included in
# all copies or substantial portions of the Software.
#
# THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND, EXPRESS OR
# IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF MERCHANTABILITY,
# FITNESS FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT. IN NO EVENT SHALL THE
# AUTHORS OR COPYRIGHT HOLDERS BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER
# LIABILITY, WHETHER IN AN ACTION OF CONTRACT, TORT OR OTHERWISE, ARISING FROM,
# OUT OF OR IN CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER DEALINGS IN
# THE SOFTWARE.

import re
import os
import sys
import cgi
import urllib2
import coverart_redirect
from os.path import splitext
from werkzeug.exceptions import NotFound
from werkzeug.wrappers import Response
from werkzeug.wsgi import pop_path_info
from coverart_redirect.utils import statuscode
from wsgiref.util import request_uri, shift_path_info

class CoverArtRedirect(object):
    ''' Handles index and redirect requests '''

    def __init__(self, config, conn):
        self.config = config
        self.conn = conn
        self.cmd = None
        self.proto = None


    def thumbnail (self, filename):
        if not '-' in filename:
            return ""

        (id, size) = filename.split ('-')

        if size.startswith ('250'):
            return "-250"
        elif size.startswith ('500'):
            return "-500"
        else:
            return ""



    def resolve_mbid (self, entity, mbid):
        """ Handle the GID redirect. Query the database to see if the
           given release has been merged into another release. If so,
           return the redirected MBID, otherwise return the original
           MBID. """

        entity = entity.replace ("-", "_")
        mbid = mbid.lower ()

        query = """
            SELECT %(entity)s.gid
              FROM musicbrainz.%(entity)s
              JOIN musicbrainz.%(entity)s_gid_redirect
                ON %(entity)s_gid_redirect.new_id = %(entity)s.id
             WHERE %(entity)s_gid_redirect.gid = %(mbid)s
        """ % ({ "entity": entity, "mbid": "%(mbid)s" })

        resultproxy = self.conn.execute (query, { "mbid": mbid })
        row = resultproxy.fetchone ()
        resultproxy.close ()
        if row:
            return row[0];

        return mbid


    def resolve_cover_index (self, mbid):
        """ Query the database to see if the given release has any
        cover art entries, if not respond with a 404 to the
        request. """

        mbid = mbid.lower ()
        query = """
            SELECT release.gid
              FROM musicbrainz.release
              JOIN cover_art_archive.cover_art ON release = release.id
             WHERE release.gid = %(mbid)s;
        """

        resultproxy = self.conn.execute (query, { "mbid": mbid })
        row = resultproxy.fetchone ()
        resultproxy.close ()
        if row:
            return row[0];

        raise NotFound ("No cover art found for release %s" % (mbid))


    def resolve_release_group_cover_art (self, mbid):
        """ This gets the selected front cover art for a release
        group, or picks the earliest front cover art available.  It
        takes a release group GID and returns a release GID -- if the
        release has front cover art.  Otherwise it raises a 404
        NotFound exception. """

        mbid = mbid.lower ()
        query = """
            SELECT DISTINCT ON (release.release_group)
                   musicbrainz.release.gid AS mbid
              FROM cover_art_archive.index_listing
              JOIN musicbrainz.release
                ON musicbrainz.release.id = cover_art_archive.index_listing.release
   FULL OUTER JOIN cover_art_archive.release_group_cover_art
                ON release_group_cover_art.release = musicbrainz.release.id
              JOIN musicbrainz.release_group
                ON musicbrainz.release_group.id = musicbrainz.release.release_group
             WHERE release_group.gid = %(mbid)s
               AND is_front = true
          ORDER BY release.release_group, release_group_cover_art.release,
                   release.date_year, release.date_month, release.date_day;
        """

        resultproxy = self.conn.execute (query, { "mbid": mbid })
        row = resultproxy.fetchone ()
        resultproxy.close ()
        if row:
            return row[0];

        raise NotFound ("No cover art found for release group %s" % (mbid))


    def resolve_cover(self, mbid, type, thumbnail):
        '''Get the frontiest or backiest cover image.'''

        query = """
            SELECT cover_art.id
              FROM cover_art_archive.cover_art
              JOIN musicbrainz.release ON release = release.id
              JOIN cover_art_archive.cover_art_type ON cover_art.id = cover_art_type.id
              JOIN cover_art_archive.art_type ON cover_art_type.type_id = art_type.id
             WHERE release.gid = %(mbid)s
               AND art_type.name = %(type)s
          ORDER BY ordering ASC LIMIT 1;
        """

        resultproxy = self.conn.execute (query, { "mbid": mbid, "type": type })
        row = resultproxy.fetchone ()
        resultproxy.close ()
        if row:
            return unicode(row[0]) + thumbnail + u".jpg"

        typestr = type.lower ()
        raise NotFound ("No %s cover image found for release with identifier %s" % (
            typestr, mbid))


    def resolve_image_id(self, mbid, filename, thumbnail):
        '''Get a cover image by image id.'''

        query = """
            SELECT cover_art.id
              FROM cover_art_archive.cover_art
              JOIN musicbrainz.release ON release = release.id
             WHERE release.gid = %(mbid)s
               AND cover_art.id = %(image_id)s
          ORDER BY ordering ASC LIMIT 1;
        """

        possible_id = re.sub ("[^0-9].*", "", filename)

        image_id = None
        try:
            image_id = int(possible_id)
        except ValueError:
            raise NotFound ("id %s is not a valid cover image id" % (name))

        resultproxy = self.conn.execute (
            query, { "mbid": mbid, "image_id": int(image_id) })
        row = resultproxy.fetchone ()
        resultproxy.close ()
        if row:
            return unicode(row[0]) + thumbnail + u".jpg"

        raise NotFound ("cover image with id %s not found" % (image_id))


    def handle_index(self):
        '''Serve up the one static index page'''

        try:
            f = open(os.path.join(self.config.static_path, "index"))
        except IOError:
            return Response(status=500, response="Internal Server Error")

        txt = f.read()
        f.close()

        return Response (response=txt, mimetype='text/html')


    def handle_dir(self, request, mbid):
        '''When the user requests no file, redirect to the root of the bucket to give the user an
           index of what is in the bucket'''

        index_url = "%s/mbid-%s/index.json" % (self.config.s3.prefix, mbid)
        return request.redirect (code=307, location=index_url)


    def handle_release (self, request, mbid, filename):
        if not filename:
            mbid = self.resolve_cover_index (mbid)
            return self.handle_dir(request, mbid)

        if filename.startswith ('front'):
            filename = self.resolve_cover (mbid, 'Front', self.thumbnail (filename))
        elif filename.startswith ('back'):
            filename = self.resolve_cover (mbid, 'Back', self.thumbnail (filename))
        else:
            filename = self.resolve_image_id (
                mbid, filename, self.thumbnail (filename))

        return self.handle_redirect(request, mbid, filename.encode('utf8'))


    def handle_release_group (self, request, mbid, filename):
        release_mbid = self.resolve_release_group_cover_art (mbid)
        if not filename:
            return self.handle_dir (request, release_mbid)
        elif filename.startswith ('front'):
            filename = self.resolve_cover (
                release_mbid, 'Front', self.thumbnail (filename))
            return self.handle_redirect (
                request, release_mbid, filename.encode('utf8'))
        else:
            return Response(status=400, response=
                            "%s not supported for release groups." % (filename))


    def handle_redirect(self, request, mbid, filename):
        """ Handle the 307 redirect. """

        if not filename:
            return [statuscode (400), "no filename specified"]

        filename = filename.replace("-250.jpg", "_thumb250.jpg")
        filename = filename.replace("-500.jpg", "_thumb500.jpg")

        url = "%s/mbid-%s/mbid-%s-%s" % (self.config.s3.prefix, mbid, mbid, filename)
        return request.redirect (code=307, location=url)


    def handle(self, request):
        '''Handle a request, parse and validate arguments and dispatch the request'''

        entity = pop_path_info(request.environ)
        if not entity:
            return self.handle_index()

        if entity not in [ 'release', 'release-group' ]:
            return Response (
                status=400, response=
                "Only release and release-group entities are currently supported")

        req_mbid = shift_path_info(request.environ)
        if not req_mbid:
            return Response (status=400, response="no MBID specified.")
        if not re.match('[a-f0-9]{8}-[a-f0-9]{4}-[a-f0-9]{4}-[a-f0-9]{4}-[a-f0-9]{12}$', req_mbid):
            return Response (status=400, response="invalid MBID specified.")

        mbid = self.resolve_mbid (entity, req_mbid)
        filename = pop_path_info(request.environ)

        if entity == 'release-group':
            return self.handle_release_group (request, mbid, filename)
        else:
            return self.handle_release (request, mbid, filename)
