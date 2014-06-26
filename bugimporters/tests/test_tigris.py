import datetime
import os

import bugimporters.tigris
import bugimporters.tests
import bugimporters.main

HERE = os.path.dirname(os.path.abspath(__file__))


def sample_data_path(f):
    return os.path.join(HERE, 'sample-data', 'tigris', f)


class FakeDate(datetime.datetime):
    """ Class to mock datetime.datetime.utcnow().
    """
    @classmethod
    def utcnow(cls):
        return cls(2013, 10, 1)


class TestCustomBugParser(object):
    @staticmethod
    def assertEqual(x, y):
        assert x == y

    ### First, test that if we create the bug importer correctly, the
    ### right thing would happen.
    def test_tigris_bug_importer_uses_tigris_parser_by_default(self):
        bbi = bugimporters.tigris.TigrisBugImporter(
            tracker_model=None, reactor_manager=None,
            bug_parser=None)
        self.assertEqual(bbi.bug_parser,
                         bugimporters.tigris.TigrisBugParser)

    def test_tigris_bug_importer_accepts_bug_parser(self):
        class SpecialTigrisImportParser:
            def __init__(self):
                self.name = "dummy"

        bbi = bugimporters.tigris.TigrisBugImporter(
            tracker_model=None, reactor_manager=None,
            bug_parser=SpecialTigrisImportParser)
        self.assertEqual(bbi.bug_parser, SpecialTigrisImportParser)


class TestTigrisBinProbe(object):

    def setup_class(cls):
        # Test data array...
        cls.data = []

    def entry_exists(self, idx):
        """ Return whether the item with the given
            index (1-based!) exists, or not.
        """
        if (idx - 1) < 0:
            return False
        if idx > len(self.data):
            return False
        if self.data[idx - 1] == 1:
            return True
        return False

    def test_binprobe(self):
        # Start value for step size, ensures that
        # the test data array always contains enough
        # values...
        BSEARCH_STEP_SIZE = 4
        # Test ranges between BSEARCH_STEP_SIZE (min) and this value (max)...
        MAX_TEST_STEP_SIZE = 120

        errs = 0
        tbi = bugimporters.tigris.TigrisBugImporter(tracker_model=None,
                                                    reactor_manager=None,
                                                    bug_parser=None)
        while (BSEARCH_STEP_SIZE < MAX_TEST_STEP_SIZE):
            # Test step size
            for l in xrange(4, 4 * BSEARCH_STEP_SIZE):
                self.data = []
                # Create data array of varying length
                for d in xrange(l - 1):
                    self.data.append(1)

                # Now try to find the index of
                # the last existing "page"
                id = 1
                while self.entry_exists(id):
                    id += BSEARCH_STEP_SIZE

                # Start the binary search
                left = id - BSEARCH_STEP_SIZE
                right = id - 1
                res = tbi.binprobe(left, right, self.entry_exists)

                if (res != l - 1):
                    errs += 1

            BSEARCH_STEP_SIZE += 1

        assert errs == 0


class TestTigrisBugImporter(object):
    def assert_(self, a):
        assert a

    def assertEqual(self, a, b):
        assert a == b

    def setup_class(cls):
        # Set up the TigrisTrackerModels that will be used here.
        cls.tm = dict(
            tracker_name='SCons',
            base_url='http://scons.tigris.org/issues/',
            bug_project_name_format='{tracker_name}',
            bitesized_type='key',
            bitesized_text='Easy',
            documentation_type='key,comp',
            documentation_text='documentation',
            bugimporter='tigris',
            queries=[
                'http://scons.tigris.org/issues/xml.cgi',
            ],
        )
        spider = bugimporters.main.BugImportSpider()
        spider.input_data = [cls.tm]
        bug_importer_and_objs = list(spider.get_bugimporters())
        assert len(bug_importer_and_objs) == 1
        obj, bug_importer = bug_importer_and_objs[0]
        cls.bug_importer = bug_importer

    def test_no_bug_found(self):
        # Parse XML document as if we got it from the web
        with open(sample_data_path('no_bug.xml')) as f:
            all_bugs = list(self.bug_importer.handle_bug_xml(f.read()))

        assert len(all_bugs) == 0

    def test_bug_attributes(self):
        # Parse XML document as if we got it from the web
        with open(sample_data_path('2946.xml')) as f:
            all_bugs = list(self.bug_importer.handle_bug_xml(f.read()))

        assert len(all_bugs) == 1
        bug = all_bugs[0]
        self.assertEqual(bug['title'], "Switching to argparse")
        self.assertEqual(
            bug['description'],
            ("With the new floor for the core sources being Python 2.7, the "
             "optparse module \nthat gets used for command-line parsing is "
             "regarded to be deprecated.\n\nIt should get rewritten to use "
             "the argparse module instead.")
        )
        self.assertEqual(bug['status'], 'RESOLVED')
        self.assertEqual(bug['importance'], 'P4')
        self.assertEqual(bug['people_involved'], 1)
        self.assertEqual(bug['date_reported'],
                         datetime.datetime(2014, 4, 27, 3, 3, 2).isoformat())
        self.assertEqual(bug['last_touched'],
                         datetime.datetime(2014, 4, 27, 3, 52, 51).isoformat())
        self.assertEqual(bug['submitter_username'], 'dirkbaechle')
        self.assertEqual(bug['submitter_realname'], '')
        self.assertEqual(bug['canonical_bug_link'],
                         'http://scons.tigris.org/issues/show_bug.cgi?id=2946')
        self.assert_(not bug['good_for_newcomers'])
        self.assert_(bug['looks_closed'])

    def test_multiple_bug(self):
        # Parse XML document as if we got it from the web
        with open(sample_data_path('multiple.xml')) as f:
            all_bugs = list(self.bug_importer.handle_bug_xml(f.read()))

        assert len(all_bugs) == 3
        # "Easy" bug
        bug = all_bugs[0]
        self.assert_(bug['good_for_newcomers'])
        self.assert_(not bug['looks_closed'])
        self.assertEqual(bug['people_involved'], 2)

        # Documentation bug (has keyword "documentation")
        bug = all_bugs[1]
        self.assert_(bug['good_for_newcomers'])
        self.assert_(bug['concerns_just_documentation'])
        self.assert_(bug['looks_closed'])

        # Documentation bug (has component "documentation")
        bug = all_bugs[2]
        self.assert_(not bug['good_for_newcomers'])
        self.assert_(bug['concerns_just_documentation'])
        self.assert_(not bug['looks_closed'])
