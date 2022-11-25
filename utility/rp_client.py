"""
This Utility will push the results to and get Report Portal launcher details.
It accepts config file location and payload_directory or launch id
In config file:
    We will have the details of report portal and launch details
In Payload_directory:
    We will have the zipped logs of the suites and error files if there is an error while executing the test case
Launch ID:
    This will be the launch id from report portal
Output:
    This will be generated by script which will details report portal launcher

Payload Directory Structure
payload
  |- results
      - test1.xml
      - test2.xml
  |- attachments
      - test1
         - test1.tar.gz
         - failed_test.err

config.json:
{

    "reportportal": {
        "api_token": "8ca12f8b-8a3c-4d22-9f04-1e81692b7230",
        "auto_dashboard": false,
        "host_url": "https://reportportal-rhcephqe.apps.ocp-c1.prod.psi.redhat.com/",
        "merge_launches": true,
        "project": "cephci",
        "property_filter": [".*"],
        "simple_xml": false,
        "launch": {
            "name": "RHCEPH-4.3 - tier-0-amk",
            "description": "Test executed on Thu Mar 17 06:16:24 UTC 2022\n",
            "attributes": {
                "ceph_version": "14.2.22-86",
                "rhcs": "4.3",
                "tier": "tier-0"
            }
        }
    }
}

We are going to parse the xunit files and get the details of the results and upload them.
if there are any attachments present for the xunit file in the attachments folder with xunit folder name
those files will get attached to the suite and if there are any .err files we are going to attach them to the respective
test cases based on the name of the test case.
If the .zip file not present in the attachments. for those we are just updating the results and skipping the attachments

Sample Output File:
    [
        {
            "id": 157579,
            "name": "tier-2_cephfs_test-volume-management",
            "status": "PASSED",
            "total": 26,
            "passed": 26,
            "failed": 0,
            "skipped": 0,
            "executionTime": 616,
            "url": "https://reportportal-rhcephqe.apps.ocp-c1.prod.psi.redhat.com/ui/#cephci/launches/all/6288/157579"
        },
        {
            "id": 157583,
            "name": "tier-2_rados_test-osd-rebalance-many-pools",
            "status": "FAILED",
            "total": 4,
            "passed": 3,
            "failed": 1,
            "skipped": 0,
            "executionTime": 396,
            "url": "https://reportportal-rhcephqe.apps.ocp-c1.prod.psi.redhat.com/ui/#cephci/launches/all/6288/157583"
        }
    ]

"""
import datetime
import json
import logging
import os
import re
import sys
from concurrent.futures import ThreadPoolExecutor
from functools import partial

import xmltodict
from docopt import docopt
from rp_utils.preproc import PreProcClient
from rp_utils.reportportalV1 import Launch, ReportPortalV1, RpLog
from rp_utils.xunit_xml import TestCase, TestSuite, XunitXML
from utils import create_run_dir, generate_unique_id, tfacon

from utility.retry import retry

log = logging.getLogger(__name__)
doc = """
Standard script to push all the logs from Xunit files and get launcher details from Report Portal

    Usage:
        rp_client.py --config_file <str> --payload_dir <str>
        rp_client.py --config_file <str> --launch_id <int>
        rp_client.py --config_file <str> --launch_id <int> --output <file-path>
        rp_client.py (-h | --help)

    Options:
        -h --help          Shows the command usage
        -c --config_file <str>  Config file with report portal details.
        -d --payload_dir <str>  Paylod directory where we have results and attachments
        -l --launch_id <int>    Report portal launch id
        -o --output <str>       File path to json output
"""


@retry(RuntimeError, tries=3, delay=120)
def get_launch_details(config_file, launch_id):
    """
    Get launcher details for specific launch id
    """
    args = {"config_file": config_file}
    rp_host_url = os.environ.get("RP_HOST_URL", None)
    url = "{}/ui/#cephci/launches/all/{}/{}"
    try:
        rportal = ReportPortalV1(PreProcClient((args)).configs.rp_config)
        api = "item/v2?filter.level.path=1&page.page=1&page.size=50"
        api += "&page.sort=startTime%2CASC&providerType=launch&launchId={}"
        results = rportal.api_get(api.format(launch_id))
        results = json.loads(results.content).get("content")

        testsuites = list()
        for ts in results:
            if ts.get("status") == "IN_PROGRESS":
                raise RuntimeError(
                    "Require status is not yet attained for the test cases in report portal"
                )
            testsuite = {
                "id": ts.get("id"),
                "name": ts.get("name"),
                "status": ts.get("status"),
                "total": ts.get("statistics").get("executions").get("total"),
                "passed": ts.get("statistics").get("executions").get("passed"),
                "failed": ts.get("statistics").get("executions").get("failed", 0),
                "skipped": ts.get("statistics").get("executions").get("skipped", 0),
            }
            if "endTime" in ts.keys() and "startTime" in ts.keys():
                testsuite.update(
                    {"executionTime": int(ts.get("endTime")) - int(ts.get("startTime"))}
                )
            if rp_host_url:
                testsuite.update(
                    {"url": url.format(rp_host_url, launch_id, ts.get("id"))}
                )

            testsuites.append(testsuite)

        return testsuites
    except Exception as e:
        raise e


def upload_logs(config_file, payload_dir):
    """
    Uploads the logs to Reportportal launch
    """
    args = {
        "config_file": config_file,
        "payload_dir": payload_dir,
    }
    preproc = PreProcClient((args))
    rportal = ReportPortalV1(preproc.configs.rp_config)
    results_file_dir = os.path.join(preproc.configs.payload_dir, "results")
    payload_subdir = os.listdir(preproc.configs.payload_dir)
    log.info(f"The payload dir file list is: {payload_subdir}")
    if preproc.config_file_path:
        log.info(f"Config file path is {preproc.config_file_path}")
        filename = os.path.basename(preproc.config_file_path)
        log.info(f"Remove config file path {filename} from {payload_subdir}")
        payload_subdir.remove(filename)
    result_file_list = XunitXML.get_file_list(results_file_dir)
    if not isinstance(result_file_list, list):
        log.info("The value of result_file_list is %s" % result_file_list)
        log.info(
            "ERROR: The payload directory path %s does not exist" % results_file_dir
        )
        return 1
    return_obj = {}
    with ThreadPoolExecutor() as executor:
        log.info("Processing xunit files concurrently")
        log.info(dir(rportal.service))
        launch = Launch(rportal)
        log.info(dir(preproc))
        launch.start()
        log.info(result_file_list)
        list(
            executor.map(partial(process_xml_file, preproc, rportal), result_file_list)
        )
        launch.finish()
    return_obj["launches"] = rportal.launches.list
    log.info("RETURN OBJECT: %s", return_obj)
    # Do not Remove this print statement as it is consumed in groovy file
    print(f"launch id: {launch.launch_id}")
    tfacon(launch.launch_id)
    return return_obj


def process_xml_file(preproc, rportal, fqpath):
    with open(fqpath) as xmlfd:
        log.info("Processing fqpath %s", fqpath)
        filename = os.path.basename(fqpath)
        filename_base, _ = os.path.splitext(filename)
        log.info("%s %s", filename, filename_base)

        log.info("Parsing XML...")
        xml_data = xmltodict.parse(xmlfd.read())
        xunit_xml = XunitXML(
            rportal, name=filename_base, configs=preproc._configs, xml_data=xml_data
        )
        process(xunit_xml, rportal)


def process(xmlObj, rportal):
    """Process xUnit XML data"""
    # override env var with config provided vars
    rp_host_url = os.environ.get("RP_HOST_URL", None)
    log.info("rp_host_url: %s", rp_host_url)

    # check for multiple testsuites in xUnit
    if xmlObj.xml_data.get("testsuites"):
        # get test suites list
        testsuites_object = xmlObj.xml_data.get("testsuites")
        testsuites = (
            testsuites_object.get("testsuite")
            if isinstance(testsuites_object.get("testsuite"), list)
            else [testsuites_object.get("testsuite")]
        )
    else:
        testsuites = [xmlObj.xml_data.get("testsuite")]
    rplog = RpLog(rportal)
    # create testsuite(s)
    log.info("Processing %s testsuite(s)", len(testsuites))
    for testsuite in testsuites:
        testcases = testsuite.get("testcase")
        if not testcases:
            log.info(
                "Found empty test suite Name: %s. Skipping this test suite"
                % testsuite.get("@name")
            )
            continue
        elif not isinstance(testcases, list):
            testcases = [testcases]

        tsuite = TestSuite(xmlObj.rportal, xmlObj.name, testsuite)
        tsuite.start()

        # create all testcases
        log.info("Starting testcases")
        for testcase in testcases:
            process_testcase(xmlObj, testcase, tsuite)
        log.info("\nFinished testcases")
        fqpath = os.path.join(xmlObj._configs.payload_dir, "attachments")
        if os.path.exists(f"{fqpath}/{tsuite.xml_name}/{tsuite.xml_name}.tar.gz"):
            rplog.add_attachment(
                tsuite.item_id, f"{fqpath}/{tsuite.xml_name}/{tsuite.xml_name}.tar.gz"
            )
        tsuite.finish()


def process_testcase(xunit_xml, testcase, tsuite):
    # Skip testcases which has empty name
    if testcase.get("@name") == "" or not testcase.get("@name"):
        log.info("Skipping testcase because name is empty: %s", testcase)
        return
    tcase = TestCase(
        xunit_xml.rportal,
        xunit_xml.name,
        testcase,
        configs=xunit_xml._configs,
        parent_id=tsuite.item_id,
    )
    tcase.start()
    fqpath = os.path.join(tcase._configs.payload_dir, "attachments")
    log.info(f"{fqpath}/{tcase.tc_name}.err")
    if tcase.status == "FAILED" and os.path.exists(
        f"{fqpath}/{tsuite.xml_name}/{tcase.tc_name.replace(' ', '_')}_0.err"
    ):
        with open(
            f"{fqpath}/{tsuite.xml_name}/{tcase.tc_name.replace(' ', '_')}_0.err", "r"
        ) as file:
            log_entries = list(generate_log_events(file))
            for i in log_entries:
                timestamp = int(
                    datetime.datetime.strptime(
                        i["date"], "%Y-%m-%d %H:%M:%S,%f"
                    ).strftime("%s")
                )
                tcase.rplog.add_message(
                    message=i["text"],
                    level="ERROR",
                    test_item_id=tcase.test_item_id,
                    msg_time=str(int(timestamp * 1000)),
                )
    tcase.finish()


def generate_log_events(file_handler):
    log_events = {}
    for line in file_handler:
        try:
            if line.startswith(match_start_line(line)):
                if log_events:
                    yield log_events
                log_events = {
                    "date": line.split("__")[0][:23],
                    "type": line.split("-", 5)[3],
                    "text": line.split("-", 5)[-1],
                }
            else:
                log_events["text"] += line
        except Exception:
            log.error(f"Unable to parse the below Line : {line}")
        yield log_events


def match_start_line(line):
    matched = re.match(r"\d\d\d\d-\d\d-\d\d\ \d\d:\d\d:\d\d,\d\d\d", line)
    if matched:
        matchThis = matched.group()
    else:
        matchThis = "NONE"
    return matchThis


if __name__ == "__main__":
    run_id = generate_unique_id(length=6)
    run_dir = create_run_dir(run_id)
    log_format = logging.Formatter(
        "%(asctime)s - %(levelname)s - %(name)s:%(lineno)d - %(message)s"
    )
    logfile = os.path.join(run_dir, "report_portal.log")
    handler = logging.FileHandler(logfile)
    handler.setFormatter(log_format)
    log.addHandler(handler)

    args = docopt(doc)
    log.info(f"Report portal client args are - '{args}'")

    config_file = args.get("--config_file")
    launch_id = args.get("--launch_id")
    if launch_id:
        launch_details = get_launch_details(config_file, launch_id)
        log.info(
            f"Report portal launch id '{launch_id}' details are -\n{launch_details}"
        )

        file_path = args.get("--output")
        if file_path:
            with open(file_path, "w") as outfile:
                outfile.write(json.dumps(launch_details, indent=4))

        sys.exit(0)

    payload_dir = args.get("--payload_dir")
    url_base = (
        "http://magna002.ceph.redhat.com/cephci-jenkins/" + run_dir.split("/")[-1]
        if "/ceph/cephci-jenkins" in run_dir
        else run_dir
    )
    log_url = f"{url_base}/report_portal.log"
    upload_logs(config_file, payload_dir)
    log.info(f"Log Location {log_url}")
