import csv
import os
import mock

import autoresponse

from bugimporters import google
from bugimporters.main import BugImportSpider


HERE = os.path.dirname(os.path.abspath(__file__))


class TestGoogleURLUtilsTestCase(object):

    def test_google_name_from_url(self):
        retval = google.google_name_from_url('http://code.google.com/p/foo')
        assert 'foo' == retval

    def test_google_bug_detail_url(self):
        expected = 'https://code.google.com/p/foo/issues/detail?id=1'
        assert expected == google.google_bug_detail_url('foo', 1)


class TestGoogleBugImporter(object):

    def setup_method(self, method):
        self.tracker_model = mock.Mock()
        self.importer = google.GoogleBugImporter(self.tracker_model)

        self.response = mock.Mock()
        self.response.body = 'foobar'
        self.response.meta = {'issue': ''}
        self.response.request.url = 'http://code.google.com/p/myproj'

    @mock.patch('bugimporters.google.scrapy.http')
    def test_process_queries(self, scrapy):
        queries = ['http://example.com']
        retval = list(self.importer.process_queries(queries))

        expected_call_kwargs = {
            'url': queries[0],
            'callback': self.importer.handle_query_csv,
        }

        request = retval[0]
        request.http.Request.assertCalledWith(**expected_call_kwargs)

    def test_handle_query_csv(self):
        with mock.patch.object(self.importer, 'prepare_bug_urls') as prep:
            self.importer.handle_query_csv(self.response)
            args, kwargs = prep.call_args_list[0]
            assert args[0] == 'myproj'
            assert isinstance(args[1], csv.DictReader)

    @mock.patch('bugimporters.google.scrapy.http')
    def test_process_bugs(self, scrapy):
        bug_list = [
            ('http://code.google.com/p/foo/issues/detail?id=1', {}),
        ]

        retval = list(self.importer.process_bugs(bug_list))

        expected_call_kwargs = {
            'url': bug_list[0][0],
            'meta': {'issue': bug_list[0][1]},
            'callback': self.importer.handle_bug_html,
        }

        request = retval[0]
        request.http.Request.assertCalledWith(**expected_call_kwargs)

    def test_create_bug_dict_from_csv(self):
        project = 'myproj'
        csv_data = [
            {'ID': '1', 'foo': 'bar'},
            {'ID': '2', 'foo': 'baz'},
            {'ID': '3', 'foo': 'qux'},
        ]

        expected = {
            'https://code.google.com/p/myproj/issues/detail?id=1': csv_data[0],
            'https://code.google.com/p/myproj/issues/detail?id=2': csv_data[1],
            'https://code.google.com/p/myproj/issues/detail?id=3': csv_data[2],
        }

        retval = self.importer._create_bug_dict_from_csv(project, csv_data)

        assert len(retval) == 3
        assert retval == expected

    @mock.patch('bugimporters.google.scrapy.http')
    def test_create_bug_dict_from_csv_handles_paging(self, scrapy):
        project = 'myproj'
        example_line = ("This file is truncated to 100 out of 636 total results. "
                        "See https://example.com/ for the next set of results.")
        csv_data = [{'ID': example_line}]

        self.importer._create_bug_dict_from_csv(project, csv_data)

        expected_call_kwargs = {
            'url': 'http://example.com/',
            'callback': self.importer.handle_query_csv,
        }

        scrapy.http.Request.assertCalledWith(**expected_call_kwargs)

    def test_handle_bug_html(self):
        with mock.patch.object(google.GoogleBugParser, 'parse') as parse:
            self.importer.handle_bug_html(self.response)
            parse.assertCalledWith(self.tracker_model)

    def test_prepare_bug_urls(self):
        project = 'myproj'
        csv_data = [{'ID': 1, 'foo': 'bar'}]

        with mock.patch.object(self.importer, '_create_bug_dict_from_csv') as create_dict:
            with mock.patch.object(self.importer, 'process_bugs') as process_bugs:
                create_dict.return_value = {'foo': 'bar'}
                process_bugs.return_value = [1, 2, 3]

                retval = list(self.importer.prepare_bug_urls(project, csv_data))
                assert retval == [1, 2, 3]


class TestGoogleBugParser(object):

    def setup_method(self, method):
        self.tracker_model = mock.Mock(tracker_name='myproj',
                                       bitesized_text='Easy',
                                       bitesized_type='',
                                       documentation_text='Docs',
                                       documentation_type='')

        self.response = mock.Mock()
        self.response.body = 'foobar'
        self.response.meta = {'issue': {}}
        self.response.request.url = 'http://code.google.com/p/myproj'

        self.parser = google.GoogleBugParser(self.response)

    def test_count_people_involved(self):
        self.parser.bug_data.update({
            'Reporter': 'foo',
            'Owner': 'bar',
            'Cc': 'foo, bar, baz, qux',
        })

        assert 4 == self.parser._count_people_involved()

    def test_parse_labels(self):
        self.parser.bug_data['AllLabels'] = 'foo, bar, baz'
        assert ['foo', 'bar', 'baz'] == self.parser._parse_labels()

    def test_parse_labels_ignores_type_tags(self):
        self.parser.bug_data['AllLabels'] = 'type-foo, name-bar, baz'
        assert ['baz'] == self.parser._parse_labels()

    def test_parse_description(self):
        example = os.path.join(HERE,
                               'sample-data',
                               'google',
                               'issue-detail.html')
        self.response.body = open(example, 'r').read()

        expected = ("Implement plus and minus infty, sign(x) doesn't work for "
                    "-infty... The\nlimit code should handle +-infty correctly.")

        assert expected == self.parser._parse_description()

    def test_parse_description_removes_child_html_elements(self):
        example = os.path.join(HERE,
                               'sample-data',
                               'google',
                               'issue-detail-with-html.html')
        self.response.body = open(example, 'r').read()

        # We just need to be concerned on whether or not the test removes
        # child html, so check startswith will suffice since the example
        # data starts with a link
        expected = "https://gist.github.com/2660237"

        assert self.parser._parse_description().startswith(expected)

    def test_parse(self):
        # Get sample response data
        example = os.path.join(HERE,
                               'sample-data',
                               'google',
                               'issue-detail.html')
        self.response.body = open(example, 'r').read()

        # Get CSV contents of issue data for testing
        csv_file = os.path.join(HERE,
                                'sample-data',
                                'google',
                                'mock-issues-1.csv')

        reader = csv.DictReader(open(csv_file, 'r'))
        self.parser.bug_data = list(reader)[0]

        # Data we expect
        expected = {
            'title': 'setup/teardown support',
            'description': ("Implement plus and minus infty, sign(x) doesn't work for "
                            "-infty... The\nlimit code should handle +-infty correctly."),
            'status': 'Accepted',
            'importance': 'Low',
            'people_involved': 1,
            'date_reported': '2010-06-10T09:49:27',
            'last_touched': '2013-11-14T11:15:03',
            'submitter_username': 'kon...@gmail.com',
            'submitter_realname': '',
            'canonical_bug_link': self.parser.bug_url,
            '_project_name': 'myproj',
            '_tracker_name': 'myproj',
            'looks_closed': False,
            'good_for_newcomers': False,
            'concerns_just_documentation': False,
        }

        assert expected == self.parser.parse(self.tracker_model)

    def test_parse_labels_bitesized(self):
        self.tracker_model.bitesized_type = 'Label'
        self.tracker_model.bitesized_text = 'Easy'

        # Get sample response data
        example = os.path.join(HERE,
                               'sample-data',
                               'google',
                               'issue-detail.html')
        self.response.body = open(example, 'r').read()

        # Get CSV contents of issue data for testing
        csv_file = os.path.join(HERE,
                                'sample-data',
                                'google',
                                'mock-issues-1.csv')

        reader = csv.DictReader(open(csv_file, 'r'))
        self.parser.bug_data = list(reader)[1]

        bug = self.parser.parse(self.tracker_model)
        assert bug['good_for_newcomers']

    def test_parse_labels_documentation(self):
        self.tracker_model.documentation_type = 'Label'
        self.tracker_model.bitesized_text = 'Docs'

        # Get sample response data
        example = os.path.join(HERE,
                               'sample-data',
                               'google',
                               'issue-detail.html')
        self.response.body = open(example, 'r').read()

        # Get CSV contents of issue data for testing
        csv_file = os.path.join(HERE,
                                'sample-data',
                                'google',
                                'mock-issues-1.csv')

        reader = csv.DictReader(open(csv_file, 'r'))
        self.parser.bug_data = list(reader)[2]

        bug = self.parser.parse(self.tracker_model)
        assert bug['concerns_just_documentation']


class FooGoogleBugImport(object):
    @staticmethod
    def assertEqual(x, y):
        assert x == y

    def test_top_to_bottom(self):
        spider = BugImportSpider()
        spider.input_data = [dict(
            tracker_name='SymPy',
            google_name='sympy',
            bitesized_type='label',
            bitesized_text='EasyToFix',
            documentation_type='label',
            documentation_text='Documentation',
            bugimporter='google.GoogleBugImporter',
            queries=[
                'https://code.google.com/feeds/issues/p/sympy/issues/full?can=open&max-results=10000' +
                '&label=EasyToFix'
            ]
        )]
        url2filename = {
            'https://code.google.com/feeds/issues/p/sympy/issues/full?can=open&max-results=10000&label=EasyToFix':
                os.path.join(HERE, 'sample-data', 'google', 'label-easytofix.atom'),
        }
        ar = autoresponse.Autoresponder(url2filename=url2filename,
                                        url2errors={})
        items = ar.respond_recursively(spider.start_requests())
        assert len(items) == 74

    def test_top_to_bottom_with_bigger_project(self):
        # For this project, we found that some bugs from the past were not
        # getting refreshed.
        #
        # This is because of a subtlety of import from the Google Code bug
        # tracker.
        #
        # The get_older_bug_data query gives us all updates to bugs that have
        # taken place since that date. So if one of the bugs in
        # existing_bug_urls has been updated, we get notified of those updates.
        #
        # But if one of those bugs has *not* been updated, then Google Code
        # tells us nothing. The old behavior was that we would, therefore,
        # leave no information about that bug in the output of the crawl.
        # Therefore, consumers of the data would conclude that the bug has
        # not been polled. But actually, we *do* have some information we
        # can report. Namely, since there was no update to the bug since
        # its last_polled, it has stayed the same until now.
        #
        # Therefore, this test verifies that we report on existing_bug_urls
        # to indicate there is no change.
        spider = bugimporters.main.BugImportSpider()
        spider.input_data = [
            {'bitesized_text': u'Effort-Minimal,Effort-Easy,Effort-Fair',
             'bitesized_type': u'label',
             'bugimporter': 'google',
             'custom_parser': u'',
             'documentation_text': u'Component-Docs',
             'documentation_type': u'label',
             'existing_bug_urls': [
                    # No data in the feed
                    u'http://code.google.com/p/soc/issues/detail?id=1461',
                    # Has data in the feed
                    u'http://code.google.com/p/soc/issues/detail?id=1618',
                    ],
             'get_older_bug_data':
                 u'https://code.google.com/feeds/issues/p/soc/issues/full?max-results=10000&can=all&updated-min=2012-05-22T19%3A52%3A10',
             'google_name': u'soc',
             'queries': [],
             'tracker_name': u'Melange'},
            ]

        url2filename = {
            'https://code.google.com/feeds/issues/p/soc/issues/full?max-results=10000&can=all&updated-min=2012-05-22T19%3A52%3A10':
                os.path.join(HERE, 'sample-data', 'google', 'soc-date-query.atom'),
            }
        ar = autoresponse.Autoresponder(url2filename=url2filename,
                                        url2errors={})
        items = ar.respond_recursively(spider.start_requests())

        # Make sure bugs that actually have data come back, clear and true
        bug_with_data = [
            x for x in items
            if (x['canonical_bug_link'] ==
                'http://code.google.com/p/soc/issues/detail?id=1618')][0]
        assert bug_with_data['title']
        assert not bug_with_data.get('_no_update', False)

        # Verify (here's the new bit) that we report on bugs that are not
        # represented in the feed.
        bug_without_data = [
            x for x in items
            if (x['canonical_bug_link'] ==
                'http://code.google.com/p/soc/issues/detail?id=1461')][0]
        assert bug_without_data['_no_update']

        assert ('http://code.google.com/p/soc/issues/detail?id=1461' in [
            x['canonical_bug_link'] for x in items])

    def test_old_bug_data(self):
        spider = bugimporters.main.BugImportSpider()
        spider.input_data = [dict(
                    tracker_name='SymPy',
                    google_name='sympy',
                    bitesized_type='label',
                    bitesized_text='EasyToFix',
                    documentation_type='label',
                    documentation_text='Documentation',
                    bugimporter = 'google.GoogleBugImporter',
                    queries=[],
                    get_older_bug_data=('https://code.google.com/feeds/issues/p/sympy/issues/full' +
                                        '?max-results=10000&can=all&updated-min=2012-09-15T00:00:00'),
                    existing_bug_urls=[
                    'http://code.google.com/p/sympy/issues/detail?id=2371',
                    ],
                    )]
        url2filename = {
            ('https://code.google.com/feeds/issues/p/sympy/issues/full' +
             '?max-results=10000&can=all&updated-min=2012-09-15T00:00:00'):
                os.path.join(HERE, 'sample-data', 'google',
                             'issues-by-date.atom'),
            }
        ar = autoresponse.Autoresponder(url2filename=url2filename,
                                        url2errors={})
        items = ar.respond_recursively(spider.start_requests())
        assert len(items) == 1
        item = items[0]
        assert item['canonical_bug_link'] == 'http://code.google.com/p/sympy/issues/detail?id=2371'

    def test_create_google_data_dict_with_everything(self):
        atom_dict = {
                'id': {'text': 'http://code.google.com/feeds/issues/p/sympy/issues/full/1215'},
                'published': {'text': '2008-11-24T11:15:58.000Z'},
                'updated': {'text': '2009-12-06T23:01:11.000Z'},
                'title': {'text': 'fix html documentation'},
                'content': {'text': """http://docs.sympy.org/modindex.html

I don't see for example the solvers module"""},
                'author': {'name': {'text': 'fabian.seoane'}},
                'cc': [
                    {'username': {'text': 'asmeurer'}}
                    ],
                'owner': {'username': {'text': 'Vinzent.Steinberg'}},
                'label': [
                    {'text': 'Type-Defect'},
                    {'text': 'Priority-Critical'},
                    {'text': 'Documentation'},
                    {'text': 'Milestone-Release0.6.6'}
                    ],
                'state': {'text': 'closed'},
                'status': {'text': 'Fixed'}
                }
        bug_atom = ObjectFromDict(atom_dict, recursive=True)
        gbp = google.GoogleBugParser(
                bug_url='http://code.google.com/p/sympy/issues/detail?id=1215')
        gbp.bug_atom = bug_atom

        got = gbp.get_parsed_data_dict(MockGoogleTrackerModel)
        wanted = {'title': 'fix html documentation',
                  'description': """http://docs.sympy.org/modindex.html

I don't see for example the solvers module""",
                  'status': 'Fixed',
                  'importance': 'Critical',
                  'people_involved': 3,
                  'date_reported': (
                datetime.datetime(2008, 11, 24, 11, 15, 58).isoformat()),
                  'last_touched': (
                datetime.datetime(2009, 12, 06, 23, 01, 11).isoformat()),
                  'looks_closed': True,
                  'submitter_username': 'fabian.seoane',
                  'submitter_realname': '',
                  'canonical_bug_link': 'http://code.google.com/p/sympy/issues/detail?id=1215',
                  'good_for_newcomers': False,
                  'concerns_just_documentation': True,
                  '_project_name': 'SymPy',
                  }
        self.assertEqual(wanted, got)

    def test_create_google_data_dict_author_in_list(self):
        atom_dict = {
                'id': {'text': 'http://code.google.com/feeds/issues/p/sympy/issues/full/1215'},
                'published': {'text': '2008-11-24T11:15:58.000Z'},
                'updated': {'text': '2009-12-06T23:01:11.000Z'},
                'title': {'text': 'fix html documentation'},
                'content': {'text': """http://docs.sympy.org/modindex.html

I don't see for example the solvers module"""},
                'author': [{'name': {'text': 'fabian.seoane'}}],
                'cc': [
                    {'username': {'text': 'asmeurer'}}
                    ],
                'owner': {'username': {'text': 'Vinzent.Steinberg'}},
                'label': [
                    {'text': 'Type-Defect'},
                    {'text': 'Priority-Critical'},
                    {'text': 'Documentation'},
                    {'text': 'Milestone-Release0.6.6'}
                    ],
                'state': {'text': 'closed'},
                'status': {'text': 'Fixed'}
                }
        bug_atom = ObjectFromDict(atom_dict, recursive=True)
        gbp = google.GoogleBugParser(
                bug_url='http://code.google.com/p/sympy/issues/detail?id=1215')
        gbp.bug_atom = bug_atom

        got = gbp.get_parsed_data_dict(MockGoogleTrackerModel)
        wanted = {'title': 'fix html documentation',
                  'description': """http://docs.sympy.org/modindex.html

I don't see for example the solvers module""",
                  'status': 'Fixed',
                  'importance': 'Critical',
                  'people_involved': 3,
                  'date_reported': (
                datetime.datetime(2008, 11, 24, 11, 15, 58).isoformat()),
                  'last_touched': (
                datetime.datetime(2009, 12, 06, 23, 01, 11).isoformat()),
                  'looks_closed': True,
                  'submitter_username': 'fabian.seoane',
                  'submitter_realname': '',
                  'canonical_bug_link': 'http://code.google.com/p/sympy/issues/detail?id=1215',
                  'good_for_newcomers': False,
                  'concerns_just_documentation': True,
                  '_project_name': 'SymPy',
                  }
        self.assertEqual(wanted, got)

    def test_create_google_data_dict_owner_in_list(self):
        atom_dict = {
                'id': {'text': 'http://code.google.com/feeds/issues/p/sympy/issues/full/1215'},
                'published': {'text': '2008-11-24T11:15:58.000Z'},
                'updated': {'text': '2009-12-06T23:01:11.000Z'},
                'title': {'text': 'fix html documentation'},
                'content': {'text': """http://docs.sympy.org/modindex.html

I don't see for example the solvers module"""},
                'author': {'name': {'text': 'fabian.seoane'}},
                'cc': [
                    {'username': {'text': 'asmeurer'}}
                    ],
                'owner': [{'username': {'text': 'Vinzent.Steinberg'}}],
                'label': [
                    {'text': 'Type-Defect'},
                    {'text': 'Priority-Critical'},
                    {'text': 'Documentation'},
                    {'text': 'Milestone-Release0.6.6'}
                    ],
                'state': {'text': 'closed'},
                'status': {'text': 'Fixed'}
                }
        bug_atom = ObjectFromDict(atom_dict, recursive=True)
        gbp = google.GoogleBugParser(
                bug_url='http://code.google.com/p/sympy/issues/detail?id=1215')
        gbp.bug_atom = bug_atom

        got = gbp.get_parsed_data_dict(MockGoogleTrackerModel)
        wanted = {'title': 'fix html documentation',
                  'description': """http://docs.sympy.org/modindex.html

I don't see for example the solvers module""",
                  'status': 'Fixed',
                  'importance': 'Critical',
                  'people_involved': 3,
                  'date_reported': (
                datetime.datetime(2008, 11, 24, 11, 15, 58).isoformat()),
                  'last_touched': (
                datetime.datetime(2009, 12, 06, 23, 01, 11).isoformat()),
                  'looks_closed': True,
                  'submitter_username': 'fabian.seoane',
                  'submitter_realname': '',
                  'canonical_bug_link': 'http://code.google.com/p/sympy/issues/detail?id=1215',
                  'good_for_newcomers': False,
                  'concerns_just_documentation': True,
                  '_project_name': 'SymPy',
                  }
        self.assertEqual(wanted, got)

    def test_create_google_data_dict_without_status(self):
        atom_dict = {
                'id': {'text': 'http://code.google.com/feeds/issues/p/sympy/issues/full/1215'},
                'published': {'text': '2008-11-24T11:15:58.000Z'},
                'updated': {'text': '2009-12-06T23:01:11.000Z'},
                'title': {'text': 'fix html documentation'},
                'content': {'text': """http://docs.sympy.org/modindex.html

I don't see for example the solvers module"""},
                'author': {'name': {'text': 'fabian.seoane'}},
                'cc': [
                    {'username': {'text': 'asmeurer'}}
                    ],
                'owner': {'username': {'text': 'Vinzent.Steinberg'}},
                'label': [
                    {'text': 'Type-Defect'},
                    {'text': 'Priority-Critical'},
                    {'text': 'Documentation'},
                    {'text': 'Milestone-Release0.6.6'}
                    ],
                'state': {'text': 'closed'},
                'status': None
                }
        bug_atom = ObjectFromDict(atom_dict, recursive=True)
        gbp = google.GoogleBugParser(
                bug_url='http://code.google.com/p/sympy/issues/detail?id=1215')
        gbp.bug_atom = bug_atom

        got = gbp.get_parsed_data_dict(MockGoogleTrackerModel)
        wanted = {'title': 'fix html documentation',
                  'description': """http://docs.sympy.org/modindex.html

I don't see for example the solvers module""",
                  'status': '',
                  'importance': 'Critical',
                  'people_involved': 3,
                  'date_reported': (
                datetime.datetime(2008, 11, 24, 11, 15, 58).isoformat()),
                  'last_touched': (
                datetime.datetime(2009, 12, 06, 23, 01, 11).isoformat()),
                  'looks_closed': True,
                  'submitter_username': 'fabian.seoane',
                  'submitter_realname': '',
                  'canonical_bug_link': 'http://code.google.com/p/sympy/issues/detail?id=1215',
                  'good_for_newcomers': False,
                  'concerns_just_documentation': True,
                  '_project_name': 'SymPy',
                  }
        self.assertEqual(wanted, got)
