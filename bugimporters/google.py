# This file is part of OpenHatch.
# Copyright (C) 2010, 2011 Jack Grigg
# Copyright (C) 2010 OpenHatch, Inc.
# Copyright (C) 2012 Berry Phillips.
#
# This program is free software: you can redistribute it and/or modify
# it under the terms of the GNU Affero General Public License as published by
# the Free Software Foundation, either version 3 of the License, or
# (at your option) any later version.
#
# This program is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
# GNU Affero General Public License for more details.
#
# You should have received a copy of the GNU Affero General Public License
# along with this program.  If not, see <http://www.gnu.org/licenses/>.

import logging
import scrapy.http

import feedparser
from gdata.projecthosting.data import IssuesFeed, IssueEntry

import bugimporters.items
from bugimporters.base import BugImporter
from bugimporters.helpers import string2naive_datetime, cached_property


class GoogleBugImporter(BugImporter):

    def __init__(self, *args, **kwargs):
        # Create a list to store bug ids obtained from queries.
        self.query_feeds = []
        # Call the parent __init__.
        super(GoogleBugImporter, self).__init__(*args, **kwargs)

    def process_queries(self, queries):
        # Add all the queries to the waiting list
        for query in queries:
            r = scrapy.http.Request(
                url=query,
                callback=self.handle_query_atom_response)
            yield r

    def handle_query_atom_response(self, response):
        just_these_bug_urls = response.meta.get('bug_list', None)
        return self.handle_query_atom(response.body, just_these_bug_urls)

    def handle_query_atom(self, query_atom, just_these_bug_urls=None):
        # Turn the query_atom into an IssuesFeed.
        query_feed = feedparser.parse(query_atom)

        # NOTE: we can probably still proceed even if the feed yells about
        if query_feed['bozo']:
            logging.warn('handle_query_atom may have failed: %s', query_feed['bozo_exception'])

        # If we learned about any bugs, go ask for data about them.
        return self.prepare_bug_urls(query_feed, just_these_bug_urls)

    def prepare_bug_urls(self, query_feed, just_these_bug_urls):
        # Convert the list of issues into a dict of bug URLs and issues.
        bug_dict = {}
        for issue in query_feed.entries:
            # Get the bug URL.
            bug_url = issue.link

            # If we were told to filter for only certain bug URLs, then
            # we look at the URL and drop the ones that do not match.
            if ((just_these_bug_urls is not None) and
                    bug_url not in just_these_bug_urls):
                continue

            # Add the issue to the bug_url_dict. This has the side-effect of
            # removing duplicate bug URLs, as later ones just overwrite earlier
            # ones.
            bug_dict[bug_url] = issue

        # And now go on to process the bug list.
        # We just use all the bugs, as they all have complete data so there is
        # no harm in updating fresh ones as there is no extra network hit.
        for parsed_bug in self.process_bugs(bug_dict.items()):
            yield parsed_bug

        # Now... if we were given a list of just_these_bug_urls, and one of them
        # didn't report an update to us, let's indicate we want process_bugs()
        # to generate a no-op report.
        if just_these_bug_urls:
            for should_hear_about in just_these_bug_urls:
                if should_hear_about in bug_dict:
                    pass  # great, we already reported about it.
                else:
                    b = bugimporters.items.ParsedBug({
                        'canonical_bug_link': should_hear_about,
                        '_no_update': True,
                    })
                    yield b

    def process_bugs(self, bug_list, older_bug_data_url=None):
        if older_bug_data_url:
            iterable = self.process_older_bugs(bug_list, older_bug_data_url)
            for item in iterable:
                yield item
            return

        for bug_url, bug_atom in bug_list:
            if bug_atom:
                # We already have the data from a query.
                yield self.handle_bug_atom(
                    bug_atom, GoogleBugParser(bug_url))
            else:
                # Fetch the bug data.
                yield scrapy.http.Request(
                    url=bug_url,
                    callback=self.handle_bug_atom)

    def process_older_bugs(self, bug_list, older_bug_data_url):
        r = scrapy.http.Request(
            url=older_bug_data_url,
            callback=self.handle_query_atom_response)
        # For historical reasons, bug_list is a tuple of (url, data).
        # We just want the URLs. self.handle_query_atom() will
        # know how to properly filter these.
        r.meta['bug_list'] = [url for (url, data) in bug_list]
        yield r

    def handle_bug_atom_response(self, response):
        # Create a GoogleBugParser instance to store the bug data.
        gbp = GoogleBugParser(response.request.url)
        return self.handle_bug_atom(response.body, gbp)

    def handle_bug_atom(self, bug_atom, gbp):
        # Pass the GoogleBugParser the Atom data
        gbp.set_bug_atom_data(bug_atom)

        # Get the parsed data dict from the GoogleBugParser
        data = gbp.get_parsed_data_dict(self.tm)
        data.update({
            'canonical_bug_link': gbp.bug_url,
            '_tracker_name': self.tm.tracker_name,
        })

        return bugimporters.items.ParsedBug(data)


class GoogleBugParser(object):
    @staticmethod
    def google_name_and_id_from_url(url):
        a, b, c, d, google_name, e, ending = url.split('/')
        show_bug, num = ending.split('=')
        bug_id = int(num)
        return (google_name, bug_id)

    def __init__(self, bug_url):
        self.bug_atom = None
        self.bug_url = bug_url
        self.google_name, self.bug_id = self.google_name_and_id_from_url(self.bug_url)

    @cached_property
    def bug_atom_url(self):
        return 'https://code.google.com/feeds/issues/p/%s/issues/full/%d' % (self.google_name, self.bug_id)

    def set_bug_atom_data(self, bug_atom):
        if type(bug_atom) == IssueEntry:
            # We have been passed the bug data directly.
            self.bug_atom = bug_atom
        else:
            # We have been passed an Atom feed string. So assume this is for a
            # single bug and parse it as an IssueEntry.
            self.bug_atom = Parse(bug_atom, IssueEntry)

    @staticmethod
    def google_count_people_involved(issue):
        # At present this only gets the author, owner if any and CCers if any.
        # FIXME: We could get absolutely everyone involved using comments,
        # but that would require an extra network call per bug.

        # Add everyone who is on CC: list
        everyone = [cc.username.text for cc in issue.cc]
        # Add author
        if type(issue.author) == type([]):
            for author in issue.author:
                everyone.append(author.name.text)
        else:
            everyone.append(issue.author.name.text)
        # Add owner if there
        if issue.owner:
            if type(issue.owner) == type([]):
                for owner in issue.owner:
                    everyone.append(owner.username.text)
            else:
                everyone.append(issue.owner.username.text)
        # Return length of the unique set of everyone.
        return len(set(everyone))

    @staticmethod
    def google_date_to_datetime(date_string):
        return string2naive_datetime(date_string)

    @staticmethod
    def google_find_label_type(labels, type_string):
        # This is for labels of format 'type-value'.
        # type is passed in, value is returned.
        for label in labels:
            if type_string in label.text:
                return label.text.split('-', 1)[1]
        return ''

    def get_parsed_data_dict(self, tm):

        issue = self.bug_atom
        if issue.status:
            status = issue.status.text
        else:
            status = ''
        if type(issue.author) == type([]):
            author = issue.author[0]
        else:
            author = issue.author

        ret_dict = bugimporters.items.ParsedBug({
                'title': issue.title.text,
                'description': issue.content.text,
                'status': status,
                'importance': self.google_find_label_type(issue.label, 'Priority'),
                'people_involved': self.google_count_people_involved(issue),
                'date_reported': self.google_date_to_datetime(issue.published.text).isoformat(),
                'last_touched': self.google_date_to_datetime(issue.updated.text).isoformat(),
                'submitter_username': author.name.text,
                'submitter_realname': '', # Can't get this from Google
                'canonical_bug_link': self.bug_url,
                '_project_name': tm.tracker_name,
                'looks_closed': (issue.state.text == 'closed')
                })

        labels = [label.text for label in issue.label]
        # Check for the bitesized keyword(s)
        if tm.bitesized_type:
            b_list = tm.bitesized_text.split(',')
            ret_dict['good_for_newcomers'] = any(b in labels for b in b_list)
        else:
            ret_dict['good_for_newcomers'] = False
        # Check whether this is a documentation bug.
        if tm.documentation_type:
            d_list = tm.documentation_text.split(',')
            ret_dict['concerns_just_documentation'] = any(d in labels for d in d_list)
        else:
            ret_dict['concerns_just_documentation'] = False

        # Then pass ret_dict out.
        return ret_dict
