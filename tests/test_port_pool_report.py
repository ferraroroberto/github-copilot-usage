"""A drained loopback port pool is reported as its own failure, not as an anonymous page error."""

from __future__ import annotations

from tests.conftest import PORT_POOL_ERROR, PORT_POOL_MESSAGE, port_pool_failure

pytest_plugins = ["pytester"]


def test_only_the_port_pool_error_gets_the_distinct_message():
    assert port_pool_failure("Page.goto: net::%s at http://127.0.0.1:1/" % PORT_POOL_ERROR) == PORT_POOL_MESSAGE
    assert port_pool_failure("Page.goto: net::ERR_CONNECTION_REFUSED at http://127.0.0.1:1/") is None
    assert port_pool_failure("assert 1 == 2") is None


def test_a_failing_run_names_the_port_pool_in_its_report_and_summary(pytester):
    pytester.makeconftest(
        "from tests.conftest import pytest_runtest_makereport, pytest_terminal_summary  # noqa: F401\n"
    )
    pytester.makepyfile(
        """
        def test_drained():
            raise RuntimeError("Page.goto: net::ERR_ADDRESS_IN_USE at http://127.0.0.1:1/")

        def test_other():
            raise RuntimeError("Page.goto: net::ERR_CONNECTION_REFUSED")
        """
    )
    result = pytester.runpytest_inprocess("-q", "-p", "no:cacheprovider")
    result.assert_outcomes(failed=2)
    out = result.stdout.str()
    assert out.count("PORT POOL EXHAUSTED") == 2  # the failing test's report and the closing summary, not test_other
    result.stdout.fnmatch_lines(["*port pool*", "*PORT POOL EXHAUSTED*", "*test_drained"])
