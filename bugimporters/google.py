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

import csv
import re

from cStringIO import StringIO
from urlparse import urlparse

import scrapy.http
import scrapy.selector

from bugimporters.base import BugImporter
from bugimporters.items import ParsedBug
from bugimporters.helpers import string2naive_datetime


def google_name_from_url(url):
    """
    Returns a google project name from a code.google.com url
    which will be something like /p/<project>
    """
    parsed = urlparse(url)
    return parsed.path.split('/')[2]


def google_bug_detail_url(project_name, bug_id):
    return 'https://code.google.com/p/{project}/issues/detail?id={id}'.format(
        project=project_name, id=bug_id
    )


class GoogleBugImporter(BugImporter):

    def process_queries(self, queries):
        """
        Process root queries for CSV data. We may need to have successive
        accesses if there are additional records to be fetched

        :param queries: a list of google issue CSV URLs
        """
        # Add all the queries to the waiting list
        for query in queries:
            yield scrapy.http.Request(url=query,
                                      callback=self.handle_query_csv)

    def handle_query_csv(self, response):
        """
        A callback for scrapy http requests for google issue CSV data

        :param response: :class:`scrapy.http.Response`
        """
        project_name = google_name_from_url(response.request.url)

        # Turn the content into a csv reader
        query_csv = csv.DictReader(StringIO(response.body))

        # If we learned about any bugs, go ask for data about them.
        return self.prepare_bug_urls(project_name, query_csv)

    def _create_bug_dict_from_csv(self, project_name, csv_data):
        """
        Creates bug data dictionary for a given project name and a CSV
        DictReader. The result will be a mapping of google issue detail
        URL -> full CSV data for the bug

        :param project_name: The google project name
        :param csv_data: a csv.DictReader object (or an iterable of dicts)
        :returns: dict mapping of detail URL -> CSV issue data
        """
        bug_dict = {}

        for issue in csv_data:
            bug_id = issue['ID']

            # Check if this line is a notice about CSV pagination
            if not re.match(r'^\d+$', str(bug_id)):
                next_url = re.findall(r'See (.+?) for the next set of results', bug_id)[0]
                scrapy.http.Request(url=next_url, callback=self.handle_query_csv)
                continue

            # Get the bug URL.
            bug_url = google_bug_detail_url(project_name, bug_id)

            # Add the issue to the bug_url_dict. This has the side-effect of
            # removing duplicate bug URLs, as later ones just overwrite earlier
            # ones.
            bug_dict[bug_url] = issue

        return bug_dict

    def prepare_bug_urls(self, project_name, csv_data):
        """
        Prepare a mapping of URL -> issue data. This will yield successive
        parsed bugs
        """
        # Convert the list of issues into a dict of bug URLs and issues.
        bug_dict = self._create_bug_dict_from_csv(project_name, csv_data)

        # And now go on to process the bug list.
        # We just use all the bugs, as they all have complete data so there is
        # no harm in updating fresh ones as there is no extra network hit.
        for parsed_bug in self.process_bugs(bug_dict.items()):
            yield parsed_bug

    def process_bugs(self, bug_list):
        for bug_url, issue_data in bug_list:
            req = scrapy.http.Request(url=bug_url,
                                      meta={'issue': issue_data},
                                      callback=self.handle_bug_html)
            yield req

    def handle_bug_html(self, response):
        """
        Callback for scraping the google bug detail page
        """
        parser = GoogleBugParser(response)
        return parser.parse(self.tm)


class GoogleBugParser(object):

    def __init__(self, response):
        self.response = response

        self.bug_url = response.request.url
        self.bug_data = response.meta['issue']

        # This will hold the parsed bug data
        self.bug = {}

    def _count_people_involved(self):
        """
        Count the total number of people involved with this bug. At present
        this only gets the author, owner if any and CCers if any. We could
        get absolutely everyone involved using comments, but that would
        require an extra network call per bug.
        """
        strip = lambda s: s.strip()

        # Start with the reporter(s)
        everyone = map(strip, self.bug_data['Reporter'].split(','))

        # Add owner(s)
        owners = map(strip, self.bug_data['Owner'].split(','))
        everyone.extend(owners)

        # Add everyone who is on CC: list
        cc = map(strip, self.bug_data['Cc'].split(','))
        everyone.extend(cc)

        # Return length of the unique set of everyone.
        return len(set(filter(bool, everyone)))

    def _parse_labels(self):
        """
        Parse bug labels from the issue data. Labels will be a comma
        delimited list of string that are either of the form <type>-<name>
        or <label>. This method will parse and return the later
        """
        labels = []

        # This is for labels of format 'type-value'.
        # type is passed in, value is returned.
        for label in self.bug_data['AllLabels'].split(','):
            label = label.strip()

            try:
                type, name = label.split('-', 1)
            except ValueError:
                labels.append(label)

        return labels

    def _parse_description(self):
        """
        Parse the full description from the bug detail page

        :returns: bug description string
        """
        # NOTE: Using the combination of text/type makes for better testing
        selector = scrapy.selector.Selector(text=self.response.body, type='html')

        # This mysterious bit will get all the text for the item description
        # but will also make sure we don't have any html tags
        xpath = '//div[contains(@class, "issuedescription")]/pre/descendant-or-self::*/text()'
        desc = ''.join(selector.xpath(xpath).extract())

        # Remove stray HTML tags
        return desc.strip('\n')

    def parse(self, tracker_model):
        """
        Parses the HTML detail page of a google code issue and combines that
        with CSV data to build a ParsedBug object.

        :param tracker_model: the tracker model used for this import
        :returns: :class:`bugimporters.items.ParsedBug`
        """
        # Build the base bug dict
        data = {
            'title': self.bug_data['Summary'],
            'description': self._parse_description(),
            'status': self.bug_data['Status'],
            'importance': self.bug_data['Priority'],
            'people_involved': self._count_people_involved(),
            'date_reported': string2naive_datetime(self.bug_data['Opened']).isoformat(),
            'last_touched': string2naive_datetime(self.bug_data['Modified']).isoformat(),
            'submitter_username': self.bug_data['Reporter'],
            'submitter_realname': '',  # Can't get this from Google
            'canonical_bug_link': self.bug_url,
            '_project_name': tracker_model.tracker_name,
            '_tracker_name': tracker_model.tracker_name,
            'looks_closed': (self.bug_data['Closed'] != ''),
            'good_for_newcomers': False,
            'concerns_just_documentation': False,
        }

        labels = self._parse_labels()

        # Check for the bitesized keyword(s)
        if tracker_model.bitesized_type:
            b_list = tracker_model.bitesized_text.split(',')
            data['good_for_newcomers'] = any(b in labels for b in b_list)

        # Check whether this is a documentation bug.
        if tracker_model.documentation_type:
            d_list = tracker_model.documentation_text.split(',')
            data['concerns_just_documentation'] = any(d in labels for d in d_list)

        return ParsedBug(data)
