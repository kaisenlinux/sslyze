from collections import Counter
from concurrent.futures._base import as_completed
from unittest import mock

import pytest

from sslyze.errors import TlsHandshakeTimedOut
from sslyze.plugins.scan_commands import ScanCommand
from sslyze.scanner import Scanner, ScanCommandErrorReasonEnum, ServerScanRequest
from sslyze.server_connectivity import ServerConnectivityTester
from sslyze.server_setting import ServerNetworkLocationViaDirectConnection
from tests.factories import ServerConnectivityInfoFactory
from tests.markers import can_only_run_on_linux_64
from tests.mock_plugins import (
    MockPlugin1ScanResult,
    MockPlugin2ScanResult,
    MockPlugin1ExtraArguments,
    ScanCommandForTests,
    ScanCommandForTestsRepository,
    MockPlugin1Implementation,
)
from tests.openssl_server import LegacyOpenSslServer, ClientAuthConfigEnum


@pytest.fixture
def mock_scan_commands():
    with mock.patch("sslyze.scanner.ScanCommandsRepository", ScanCommandForTestsRepository):
        yield


class TestServerScanRequest:
    def test_with_extra_arguments_but_no_corresponding_scan_command(self):
        # When trying to queue a scan for a server
        with pytest.raises(ValueError):
            ServerScanRequest(
                server_info=ServerConnectivityInfoFactory.create(),
                # With an extra argument for one command
                scan_commands_extra_arguments={
                    ScanCommandForTests.MOCK_COMMAND_1: MockPlugin1ExtraArguments(extra_field="test")
                },
                # But that specific scan command was not queued
                scan_commands={ScanCommandForTests.MOCK_COMMAND_2},
            )
            # It fails


class TestScanner:
    def test(self, mock_scan_commands):
        # Given a server to scan
        server_scan = ServerScanRequest(
            server_info=ServerConnectivityInfoFactory.create(),
            scan_commands={ScanCommandForTests.MOCK_COMMAND_1, ScanCommandForTests.MOCK_COMMAND_2},
        )

        # When queuing the scan
        scanner = Scanner()
        scanner.queue_scan(server_scan)

        # It succeeds
        all_results = []
        for result in scanner.get_results():
            all_results.append(result)

            # And the right result is returned
            assert result.server_info == server_scan.server_info
            assert result.scan_commands == server_scan.scan_commands
            assert result.scan_commands_extra_arguments == server_scan.scan_commands_extra_arguments
            assert len(result.scan_commands_results) == 2

            assert type(result.scan_commands_results[ScanCommandForTests.MOCK_COMMAND_1]) == MockPlugin1ScanResult
            assert type(result.scan_commands_results[ScanCommandForTests.MOCK_COMMAND_2]) == MockPlugin2ScanResult

        assert len(all_results) == 1

    def test_duplicate_server(self, mock_scan_commands):
        # Given a server to scan
        server_info = ServerConnectivityInfoFactory.create()

        # When trying to queue two scans for this server
        server_scan1 = ServerScanRequest(server_info=server_info, scan_commands={ScanCommandForTests.MOCK_COMMAND_1})
        server_scan2 = ServerScanRequest(server_info=server_info, scan_commands={ScanCommandForTests.MOCK_COMMAND_2})
        scanner = Scanner()
        scanner.queue_scan(server_scan1)

        # It fails
        with pytest.raises(ValueError):
            scanner.queue_scan(server_scan2)

    def test_with_extra_arguments(self, mock_scan_commands):
        # Given a server to scan with a scan command
        server_scan = ServerScanRequest(
            server_info=ServerConnectivityInfoFactory.create(),
            scan_commands={ScanCommandForTests.MOCK_COMMAND_1},
            # And the command takes an extra argument
            scan_commands_extra_arguments={
                ScanCommandForTests.MOCK_COMMAND_1: MockPlugin1ExtraArguments(extra_field="test")
            },
        )

        # When queuing the scan
        scanner = Scanner()
        scanner.queue_scan(server_scan)

        # It succeeds
        all_results = []
        for result in scanner.get_results():
            all_results.append(result)

            # And the extra argument was taken into account
            assert result.scan_commands_extra_arguments == server_scan.scan_commands_extra_arguments

        assert len(all_results) == 1

    def test_error_bug_in_sslyze_when_scheduling_jobs(self, mock_scan_commands):
        # Given a server to scan with some scan commands
        server_scan = ServerScanRequest(
            server_info=ServerConnectivityInfoFactory.create(),
            scan_commands={ScanCommandForTests.MOCK_COMMAND_1, ScanCommandForTests.MOCK_COMMAND_2},
        )

        # And the first scan command will trigger an error when generating scan jobs
        with mock.patch.object(MockPlugin1Implementation, "scan_jobs_for_scan_command", side_effect=RuntimeError):
            # When queuing the scan
            scanner = Scanner()
            scanner.queue_scan(server_scan)

        # It succeeds
        for result in scanner.get_results():
            # And the exception was properly caught and returned
            assert len(result.scan_commands_errors) == 1
            error = result.scan_commands_errors[ScanCommandForTests.MOCK_COMMAND_1]
            assert ScanCommandErrorReasonEnum.BUG_IN_SSLYZE == error.reason
            assert error.exception_trace

    def test_error_bug_in_sslyze_when_processing_job_results(self, mock_scan_commands):
        # Given a server to scan with some scan commands
        server_scan = ServerScanRequest(
            server_info=ServerConnectivityInfoFactory.create(),
            scan_commands={ScanCommandForTests.MOCK_COMMAND_1, ScanCommandForTests.MOCK_COMMAND_2},
        )

        # And the first scan command will trigger an error when processing the completed scan jobs
        with mock.patch.object(MockPlugin1Implementation, "_scan_job_work_function", side_effect=RuntimeError):
            # When queuing the scan
            scanner = Scanner()
            scanner.queue_scan(server_scan)

        # It succeeds
        for result in scanner.get_results():
            # And the exception was properly caught and returned
            assert len(result.scan_commands_errors) == 1
            error = result.scan_commands_errors[ScanCommandForTests.MOCK_COMMAND_1]
            assert ScanCommandErrorReasonEnum.BUG_IN_SSLYZE == error.reason
            assert error.exception_trace

    @can_only_run_on_linux_64
    def test_error_client_certificate_needed(self):
        # Given a server that requires client authentication
        with LegacyOpenSslServer(client_auth_config=ClientAuthConfigEnum.REQUIRED) as server:
            # And sslyze does NOT provide a client certificate
            server_location = ServerNetworkLocationViaDirectConnection(
                hostname=server.hostname, ip_address=server.ip_address, port=server.port
            )
            server_info = ServerConnectivityTester().perform(server_location)

            server_scan = ServerScanRequest(
                server_info=server_info,
                scan_commands={
                    # And a scan command that cannot be completed without a client certificate
                    ScanCommand.HTTP_HEADERS,
                },
            )

            # When queuing the scan
            scanner = Scanner()
            scanner.queue_scan(server_scan)

            # It succeeds
            all_results = []
            for result in scanner.get_results():
                all_results.append(result)

            assert len(all_results) == 1

            # And the error was properly returned
            error = all_results[0].scan_commands_errors[ScanCommand.HTTP_HEADERS]
            assert error.reason == ScanCommandErrorReasonEnum.CLIENT_CERTIFICATE_NEEDED

    def test_error_server_connectivity_issue_handshake_timeout(self, mock_scan_commands):
        # Given a server to scan with some commands
        server_scan = ServerScanRequest(
            server_info=ServerConnectivityInfoFactory.create(),
            scan_commands={ScanCommandForTests.MOCK_COMMAND_1, ScanCommandForTests.MOCK_COMMAND_2},
        )

        # And the first scan command will trigger a handshake timeout with the server
        with mock.patch.object(
            MockPlugin1Implementation,
            "_scan_job_work_function",
            side_effect=TlsHandshakeTimedOut(
                server_location=server_scan.server_info.server_location,
                network_configuration=server_scan.server_info.network_configuration,
                error_message="error",
            ),
        ):
            # When queuing the scan
            scanner = Scanner()
            scanner.queue_scan(server_scan)

        # It succeeds
        for result in scanner.get_results():
            # And the error was properly caught and returned
            assert len(result.scan_commands_errors) == 1
            error = result.scan_commands_errors[ScanCommandForTests.MOCK_COMMAND_1]
            assert ScanCommandErrorReasonEnum.CONNECTIVITY_ISSUE == error.reason
            assert error.exception_trace


class TestScannerInternals:
    def test(self, mock_scan_commands):
        # Given a lot of servers to scan
        total_server_scans_count = 100
        server_scans = [
            ServerScanRequest(
                server_info=ServerConnectivityInfoFactory.create(),
                scan_commands={ScanCommandForTests.MOCK_COMMAND_1, ScanCommandForTests.MOCK_COMMAND_2},
            )
            for _ in range(total_server_scans_count)
        ]

        # And a scanner with specifically chosen network settings
        per_server_concurrent_connections_limit = 4
        concurrent_server_scans_limit = 20
        scanner = Scanner(per_server_concurrent_connections_limit, concurrent_server_scans_limit)

        # When queuing the scans, it succeeds
        for scan in server_scans:
            scanner.queue_scan(scan)

        # And the right number of scans was performed
        assert total_server_scans_count == len(scanner._queued_server_scans)

        # And the chosen network settings were used
        assert concurrent_server_scans_limit == len(scanner._thread_pools)
        for pool in scanner._thread_pools:
            assert per_server_concurrent_connections_limit == pool._max_workers

        # And the server scans were evenly distributed among the thread pools to maximize performance
        expected_server_scans_per_pool = int(total_server_scans_count / concurrent_server_scans_limit)
        thread_pools_used = [server_scan.queued_on_thread_pool_at_index for server_scan in scanner._queued_server_scans]
        server_scans_per_pool_count = Counter(thread_pools_used)
        for pool_count in server_scans_per_pool_count.values():
            assert expected_server_scans_per_pool == pool_count

    def test_emergency_shutdown(self, mock_scan_commands):
        # Given a lot of servers to scan
        total_server_scans_count = 100
        server_scans = [
            ServerScanRequest(
                server_info=ServerConnectivityInfoFactory.create(),
                scan_commands={ScanCommandForTests.MOCK_COMMAND_1, ScanCommandForTests.MOCK_COMMAND_2},
            )
            for _ in range(total_server_scans_count)
        ]

        # And the scans get queued
        scanner = Scanner()
        for scan in server_scans:
            scanner.queue_scan(scan)

        # When trying to quickly shutdown the scanner, it succeeds
        scanner.emergency_shutdown()

        # And all the queued jobs were done or cancelled
        all_queued_futures = []
        for server_scan in scanner._queued_server_scans:
            all_queued_futures.extend(server_scan.all_queued_scan_jobs)
        for completed_future in as_completed(all_queued_futures):
            assert completed_future.done()
