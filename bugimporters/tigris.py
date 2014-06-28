# This file is part of OpenHatch.
# Copyright (C) 2014 Dirk Baechle
# Copyright (C) 2014 OpenHatch, Inc.
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

import lxml
import lxml.etree
import logging
import scrapy
from urllib2 import urlopen

import bugimporters.items
from bugimporters.base import BugImporter, printable_datetime
from bugimporters.helpers import cached_property, string2naive_datetime

### This Importer is based on the design of the BugzillaBugImporter.
#
### Since the tigris.org tracker doesn't seem to offer an easy way
### to get at the total number of issues, we are probing ID numbers
### before trying to download any actual data.
### We start at 1, and then progress in steps of +1024, until no
### issue is found. Then a simple binary search is used to find the
### exact upper bound, in a minimal number of steps.
#
### This "max" value is then used to download the issues, in packages
### of 50 entries per XML file.
#


class TigrisBugImporter(BugImporter):
    def __init__(self, *args, **kwargs):
        # Call the parent __init__.
        super(TigrisBugImporter, self).__init__(*args, **kwargs)

        if self.bug_parser is None:
            self.bug_parser = TigrisBugParser

        self.BSEARCH_STEP_SIZE = 1024

    def issue_exists(self, id, url):
        """ Return whether the issue page with the given
            index (1-based!) exists, or not.
            @param id Index (1-based) of the issue to test
            @param url Base URL to the project's xml.cgi (no params attached!)
            @return `True` if the issue exists, `False` if not
        """
        query_url = url + '?include_attachments=false&id=%d' % id
        try:
            issues_xml = lxml.etree.XML(urlopen(query_url).read())
            for issue in issues_xml.xpath('issue'):
                error = issue.attrib.get('status_code', None)
                if error and error == "404":
                    return False
                else:
                    return True
        except:
            pass

        return False

    def binprobe(self, left, right, index_exists):
        """ Searches the last existing entry in a
            "sequence of indices" from left to right (including).
            Assumes that "left" starts on an existing entry,
            and left <= right, and left >= 0, and right >= 0.
            The index "right" may either be the last existing entry,
            or points to an entry that doesn't exist.
            @param left Start index
            @param right End index
            @param index_exists Function that checks whether a 1-based index
                                 is in or out of the sequence (exists or not).
            @return 1-based index of the last existing entry, in
                     the given interval
        """
        while ((right - left) > 1):
            middle = left + (right - left) // 2
            if not index_exists(middle):
                right = middle - 1
            else:
                left = middle

        # Special handling for when only the two
        # last IDs are left...or a single one (left=right).
        if index_exists(right):
            return right
        return left

    def get_number_of_issues(self, url, start_id=1):
        """ Return the 1-based index of the highest available (=existing)
            issue for the given base URL, when starting to
            probe at start_id.
            @param url Base URL to the project's xml.cgi (no params attached!)
            @param start_id Index (1-based) from where to probe upwards
            @return 1-based index of the last existing issue
        """
        # Start at the given index
        id = start_id
        # Loop in large steps, until id doesn't exist
        steps = 0
        while self.issue_exists(id, url):
            id += self.BSEARCH_STEP_SIZE
            steps += 1

        if steps:
            # Start the binary search
            left = id - self.BSEARCH_STEP_SIZE
            right = id - 1
            return self.binprobe(left, right,
                                 lambda x: self.issue_exists(x, url))

        return id

    def process_queries(self, queries):
        max_id = 1
        for query_url in queries:
            # Split off start ID, if given
            start_id = 1
            qpos = query_url.find('?')
            if qpos > 0:
                qlist = query_url.split('?')
                query_url = qlist[0]
                for qparam in qlist[1:]:
                    plist = qparam.split('&')
                    for param in plist:
                        if param.startswith('id='):
                            try:
                                start_id = int(param[3:])
                            except:
                                pass
            # Get the number of issues
            last_id = self.get_number_of_issues(query_url, start_id)
            if last_id > max_id:
                max_id = last_id
        # Enqueue the work of downloading information about those
        # bugs.
        for request in self.generate_requests_for_bugs(start_id, max_id):
            yield request

    def generate_requests_for_bugs(self, start_id, max_id, AT_A_TIME=50):
        if AT_A_TIME < 1:
            AT_A_TIME = 1
        first_n = start_id
        rest = start_id + AT_A_TIME - 1
        while first_n <= max_id:
            # Ensure that no "overflow" at the end of the interval occurs
            if rest > max_id:
                rest = max_id
            # Create a single URL to fetch all the bug data.
            if first_n < rest:
                ids = '%d-%d' % (first_n, rest)
            else:
                ids = '%d' % first_n
            big_url = ('%sxml.cgi?include_attachments=false&'
                       'id=%s') % (self.tm.get_base_url(), ids)

            # Create the corresponding request object
            r = scrapy.http.Request(url=big_url,
                                    callback=self.handle_bug_xml_response)

            # Update the 'rest' of the work
            first_n += AT_A_TIME
            rest += AT_A_TIME

            # yield the Request so it actually gets handled
            yield r

    def handle_bug_xml_response(self, response):
        return self.handle_bug_xml(response.body)

    def handle_bug_xml(self, bug_list_xml_string):
        logging.info("STARTING XML")
        # Turn the string into an XML tree.
        try:
            bug_list_xml = lxml.etree.XML(bug_list_xml_string)
        except Exception:
            logging.exception("Eek, XML parsing failed. "
                              "Jumping to the errback.")
            logging.error("If this keeps happening, you might want to "
                          "delete/disable the bug tracker causing this.")
            raise

        return self.handle_bug_list_xml_parsed(bug_list_xml)

    def handle_bug_list_xml_parsed(self, bug_list_xml):
        for bug_xml in bug_list_xml.xpath('issue'):
            error = bug_xml.attrib.get('status_code', None)
            if error and error == "404":
                logging.error("Uh, there was a non-existing issue (%s)",
                              bug_xml.xpath('issue_id')[0].text)
                continue  # Skip this bug, we have an error and no data.

            # Create a TigrisBugParser with the XML data.
            bbp = self.bug_parser(bug_xml)

            # Get the parsed data dict from the TigrisBugParser.
            data = bbp.get_parsed_data_dict(
                base_url=self.tm.get_base_url(),
                bitesized_type=self.tm.bitesized_type,
                bitesized_text=self.tm.bitesized_text,
                documentation_type=self.tm.documentation_type,
                documentation_text=self.tm.documentation_text
            )

            name_format = '{tracker_name}'
            if hasattr(self.tm, 'bug_project_name_format'):
                name_format = self.tm.bug_project_name_format
            data.update({
                'canonical_bug_link': bbp.bug_url,
                '_tracker_name': self.tm.tracker_name,
                '_project_name': bbp.generate_bug_project_name(
                        bug_project_name_format=name_format,
                        tracker_name=self.tm.tracker_name),
            })

            yield data


class TigrisBugParser:
    @staticmethod
    def get_tag_text_from_xml(xml_doc, tag_name, index=0):
        """Given an object representing <issue><tag>text</tag></issue>,
        and tag_name = 'tag', returns 'text'.

        If someone carelessly passes us something else, we bail
        with ValueError."""
        if xml_doc.tag != 'issue':
            error_msg = "You passed us a %s tag." % xml_doc.tag
            error_msg += " We wanted a <issue> object."
            raise ValueError(error_msg)
        tags = xml_doc.xpath(tag_name)
        try:
            return tags[index].text or u''
        except IndexError:
            return ''

    def __init__(self, bug_xml):
        self.bug_xml = bug_xml
        self.bug_id = self._bug_id_from_bug_data()
        self.bug_url = None  # This gets filled in the data parser.

    def _bug_id_from_bug_data(self):
        return int(self.get_tag_text_from_xml(self.bug_xml, 'issue_id'))

    @cached_property
    def product(self):
        return self.get_tag_text_from_xml(self.bug_xml, 'product')

    @cached_property
    def component(self):
        return self.get_tag_text_from_xml(self.bug_xml, 'component')

    @cached_property
    def subcomponent(self):
        return self.get_tag_text_from_xml(self.bug_xml, 'subcomponent')

    @staticmethod
    def _who_tag_to_username_and_realname(who_tag):
        username = who_tag.text
        realname = who_tag.attrib.get('name', '')
        return username, realname

    @staticmethod
    def tigris_count_people_involved(xml_doc):
        """Strategy: Create a set of all the listed text values
        inside a <who ...>(text)</who> tag
        Return the length of said set."""
        everyone = [tag.text for tag in xml_doc.xpath('.//who')]
        return len(set(everyone))

    @staticmethod
    def tigris_date_to_printable_datetime(date_string):
        return string2naive_datetime(date_string).isoformat()

    def get_parsed_data_dict(self,
                             base_url, bitesized_type, bitesized_text,
                             documentation_type, documentation_text):
        # Generate the bug_url.
        self.bug_url = '%sshow_bug.cgi?id=%d' % (base_url, self.bug_id)

        xml_data = self.bug_xml

        date_reported_text = self.get_tag_text_from_xml(xml_data,
                                                        'creation_ts')
        last_touched_text = self.get_tag_text_from_xml(xml_data, 'delta_ts')
        u, r = self._who_tag_to_username_and_realname(
            xml_data.xpath('.//reporter')[0])
        status = self.get_tag_text_from_xml(xml_data, 'issue_status')
        looks_closed = status in ('RESOLVED', 'WONTFIX', 'CLOSED', 'INVALID')

        ret_dict = bugimporters.items.ParsedBug({
            'title': self.get_tag_text_from_xml(xml_data, 'short_desc'),
            'description': (self.get_tag_text_from_xml(
                xml_data, 'long_desc/thetext') or '(Empty description)'),
            'status': status,
            'importance': self.get_tag_text_from_xml(xml_data, 'priority'),
            'people_involved': self.tigris_count_people_involved(xml_data),
            'date_reported': self.tigris_date_to_printable_datetime(
                date_reported_text),
            'last_touched': self.tigris_date_to_printable_datetime(
                last_touched_text),
            'last_polled': printable_datetime(),
            'submitter_username': u,
            'submitter_realname': r,
            'canonical_bug_link': self.bug_url,
            'looks_closed': looks_closed
            })
        keywords_text = self.get_tag_text_from_xml(xml_data, 'keywords') or ''
        keywords = map(lambda s: s.strip(),
                       keywords_text.split(','))
        # Check for the bitesized keyword
        is_easy = False
        if bitesized_type:
            b_list = bitesized_text.split(',')
            if bitesized_type == 'key':
                is_easy = any(b in keywords for b in b_list)
            if not is_easy and bitesized_type == 'wboard':
                whiteboard_text = self.get_tag_text_from_xml(
                    xml_data, 'status_whiteboard')
                is_easy = any(b in whiteboard_text for b in b_list)
        ret_dict['good_for_newcomers'] = is_easy
        # Check whether this is a documentation bug.
        is_doc = False
        if documentation_type:
            d_list = documentation_text.split(',')
            if 'key' in documentation_type:
                is_doc = any(d in keywords for d in d_list)
            if not is_doc and 'comp' in documentation_type:
                is_doc = any(d == self.component for d in d_list)
            if not is_doc and 'subcomp' in documentation_type:
                is_doc = any(d == self.subcomponent for d in d_list)
            if not is_doc and 'prod' in documentation_type:
                is_doc = any(d == self.product for d in d_list)
        ret_dict['concerns_just_documentation'] = is_doc

        # And pass ret_dict on.
        return ret_dict

    def generate_bug_project_name(self, bug_project_name_format, tracker_name):
        return bug_project_name_format.format(
            tracker_name=tracker_name,
            product=self.product,
            component=self.component)
