"""Failure and recovery tests use temporary state and mocked remote services."""

from dataclasses import replace
from datetime import date
from decimal import Decimal
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import httpx
import pytest

from monarch_euro import cli, pipeline
from monarch_euro.config import load_config
from monarch_euro.fx import FxConverter
from monarch_euro.models import ConvertedTransaction, SourceTransaction
from monarch_euro.sinks.csvfile import CsvSink
from monarch_euro.sinks.monarch import MonarchError, MonarchSink
from monarch_euro.store import Store


@pytest.fixture
def config(tmp_path, monkeypatch):
    # Do not read the developer's credentials or state.
    monkeypatch.setattr('os.environ', {
        'STATE_DIR': str(tmp_path), 'WISE_ACCOUNTS': 'USD|Wise USD',
        'WISE_TOKEN': 'test-token',
    })
    return load_config(tmp_path / 'absent.env')


@pytest.fixture
def txn():
    return SourceTransaction(
        'wise-usd', 'wise:1:2:USD', date(2026, 9, 15), Decimal('-10'),
        'USD', 'Shop', 'Shop', 'ref', False, {},
    )


@pytest.fixture
def remotes(monkeypatch, txn):
    wise = MagicMock()
    wise.__enter__.return_value = wise
    wise.profiles.return_value = [{'id': 1, 'type': 'personal'}]
    wise.balances.return_value = [{'id': 2, 'currency': 'USD'}]
    wise.transactions.return_value = [txn]
    monkeypatch.setattr(pipeline, 'WiseClient', lambda **kw: wise)
    monkeypatch.setattr('monarch_euro.sources.wise.WiseClient', lambda **kw: wise)
    monarch = MagicMock()
    monarch.__enter__.return_value = monarch
    monarch.ensure_account.return_value = 'account'
    monarch.post.return_value = 'remote-id'
    monkeypatch.setattr(pipeline, 'MonarchSink', lambda **kw: monarch)
    bank = MagicMock(side_effect=AssertionError('Wise-only must not construct Enable Banking'))
    monkeypatch.setattr(pipeline, 'EnableBankingClient', bank)
    return wise, monarch


def test_wise_only_sync_and_repeat(config, remotes):
    first = pipeline.sync(config)
    second = pipeline.sync(config)
    assert first.ok and first.posted == 1
    assert second.ok and second.posted == 0 and second.skipped == 1
    assert remotes[1].post.call_count == 1
    with Store(config.db_path) as store:
        assert store.posted_count() == 1
        assert not store.pending_writes()


def test_duplicate_references_in_fetch_only_post_once(config, txn, remotes):
    remotes[0].transactions.return_value = [txn, txn]
    result = pipeline.sync(config)
    assert result.ok and result.posted == 1 and result.skipped == 1
    assert remotes[1].post.call_count == 1


def test_enable_banking_batch_deduplication(config, txn, remotes, monkeypatch):
    link = SimpleNamespace(key='n26', monarch_account_name='N26')
    config = replace(config, links=[link], wise_accounts=[])
    bank = MagicMock()
    bank.__enter__.return_value = bank
    bank.get_session.return_value = {'accounts': [{'uid': txn.account_uid}]}
    bank.transactions.return_value = [txn, txn]
    monkeypatch.setattr(pipeline, 'EnableBankingClient', lambda **kw: bank)
    with Store(config.db_path) as store:
        store.put_session('n26', 'session', 'N26', 'DE', None)
    result = pipeline.sync(config)
    assert result.ok and result.posted == 1 and result.skipped == 1
    assert remotes[1].post.call_count == 1


@pytest.mark.parametrize('failure', [TimeoutError('response lost'), RuntimeError('bad response')])
def test_uncertain_write_blocks_retry_until_resolved(config, remotes, txn, failure):
    remotes[1].post.side_effect = failure
    first = pipeline.sync(config)
    second = pipeline.sync(config)
    assert not first.ok and not second.ok
    assert remotes[1].post.call_count == 1
    with Store(config.db_path) as store:
        assert store.posted_count() == 0
        assert len(store.pending_writes()) == 1
        with store.exclusive():
            store.resolve_write(txn.dedupe_key(), 'found-in-monarch')
        assert store.posted_count() == 1
        assert not store.pending_writes()
    remotes[1].post.side_effect = None
    assert pipeline.sync(config).ok
    assert remotes[1].post.call_count == 1


def test_explicit_retry_releases_uncertain_write(config, remotes, txn):
    remotes[1].post.side_effect = TimeoutError('response lost')
    assert not pipeline.sync(config).ok
    with Store(config.db_path) as store, store.exclusive():
        store.resolve_write(txn.dedupe_key(), None)
    remotes[1].post.side_effect = None
    assert pipeline.sync(config).posted == 1


def test_commit_failure_does_not_repeat_remote_write(config, remotes, monkeypatch):
    with monkeypatch.context() as patch:
        patch.setattr(Store, 'mark_posted', MagicMock(side_effect=OSError('disk failure')))
        assert not pipeline.sync(config).ok
    assert not pipeline.sync(config).ok
    assert remotes[1].post.call_count == 1


def test_dry_run_does_not_reserve_or_mark_writes(config, remotes):
    assert pipeline.sync(replace(config, dry_run=True)).ok
    with Store(config.db_path) as store:
        assert not store.pending_writes()
        assert store.posted_count() == 0


def test_missing_id_never_enters_posted_ledger(config, remotes):
    remotes[1].post.return_value = None
    assert not pipeline.sync(config).ok
    with Store(config.db_path) as store:
        assert store.posted_count() == 0
        assert len(store.pending_writes()) == 1


@pytest.mark.parametrize('response', [{}, None, {'createTransaction': {'transaction': {}}}])
def test_monarch_sink_requires_transaction_id(tmp_path, txn, response):
    with MonarchSink('', '', '', tmp_path / 'session') as sink:
        sink._categories = {'Uncategorized': 'category'}
        sink._mm = SimpleNamespace(create_transaction=AsyncMock(return_value=response))
        converted = ConvertedTransaction(txn, txn.amount, 'USD', None, None)
        with pytest.raises(MonarchError, match='no transaction ID'):
            sink.post(converted, 'account', 'Shop', None, None)


def test_metadata_failure_finalizes_run(config, remotes):
    remotes[1].refresh_metadata.side_effect = RuntimeError('expired session')
    assert not pipeline.sync(config).ok
    with Store(config.db_path) as store:
        run = store.recent_runs()[0]
        assert run['status'] == 'error' and run['finished_at']
        assert 'expired session' in run['error']


def test_interrupt_finalizes_run(config, remotes):
    remotes[1].refresh_metadata.side_effect = KeyboardInterrupt()
    with pytest.raises(KeyboardInterrupt):
        pipeline.sync(config)
    with Store(config.db_path) as store:
        assert store.recent_runs()[0]['status'] == 'error'


def test_missing_wise_token_is_failure(config, remotes):
    result = pipeline.sync(replace(config, wise_token=''))
    assert not result.ok
    assert any('WISE_TOKEN' in error for error in result.errors)
    remotes[0].transactions.assert_not_called()


def test_overlap_is_rejected_before_remote_write(config, remotes):
    with Store(config.db_path) as store, store.exclusive():
        with pytest.raises(RuntimeError, match='Another sync'):
            pipeline.sync(config)
    remotes[1].post.assert_not_called()
    assert pipeline.sync(config).ok


def export(config):
    return cli.cmd_export(config, SimpleNamespace(days=30, new_only=True, mark=True))


def test_export_write_failure_keeps_both_ledgers_empty(config, remotes, monkeypatch):
    monkeypatch.setattr(CsvSink, 'write', MagicMock(side_effect=OSError('disk failure')))
    with pytest.raises(OSError):
        export(config)
    with Store(config.db_path) as store:
        assert store.posted_count() == 0
        assert store.conn.execute('SELECT COUNT(*) FROM exported').fetchone()[0] == 0


def test_export_is_separate_from_posted_and_retains_files(config, remotes, txn):
    assert export(config) == 0
    first = next((config.state_dir / 'exports').glob('*.csv'))
    original = first.read_bytes()
    assert export(config) == 0
    assert len(list(first.parent.glob('*.csv'))) == 1
    remotes[0].transactions.return_value = [replace(txn, reference='second')]
    assert export(config) == 0
    assert len(list(first.parent.glob('*.csv'))) == 2
    assert first.read_bytes() == original
    with Store(config.db_path) as store:
        assert store.posted_count() == 0
    # Exporting a file alone must not suppress a live import.
    remotes[0].transactions.return_value = [txn]
    assert pipeline.sync(config).posted == 1


def test_export_publish_failure_cleans_temporary_file(config, remotes, monkeypatch):
    monkeypatch.setattr('monarch_euro.sinks.csvfile.os.replace',
                        MagicMock(side_effect=OSError('publish failure')))
    with pytest.raises(OSError):
        export(config)
    assert not list((config.state_dir / 'exports').iterdir())


def test_weekday_cache_miss_fetches_authoritative_rate(tmp_path):
    requests = []
    def handler(request):
        requests.append(request)
        return httpx.Response(200, json={'date': '2026-09-15', 'rates': {'USD': 1.2}})
    with Store(tmp_path / 'db') as store, httpx.Client(transport=httpx.MockTransport(handler)) as client:
        store.put_fx_rate('EUR', 'USD', date(2026, 9, 14), Decimal('1.1'), date(2026, 9, 14))
        with FxConverter(store, 'USD', client=client) as fx:
            assert fx.rate_for('EUR', date(2026, 9, 15)) == (Decimal('1.2'), date(2026, 9, 15))
    assert len(requests) == 1


def test_fx_prefetch_failure_does_not_use_stale_weekday_rate(tmp_path, txn):
    def handler(request):
        if '..' in request.url.path:
            return httpx.Response(503)
        return httpx.Response(200, json={'date': '2026-09-15', 'rates': {'USD': 1.2}})
    with Store(tmp_path / 'db') as store, httpx.Client(transport=httpx.MockTransport(handler)) as client:
        store.put_fx_rate('EUR', 'USD', date(2026, 9, 14), Decimal('1.1'), date(2026, 9, 14))
        with FxConverter(store, 'USD', client=client) as fx:
            txn = replace(txn, currency='EUR')
            fx.prefetch([txn])
            assert fx.convert(txn).amount == Decimal('-12.00')


def test_confirmed_export_is_skipped_by_sync(config, remotes):
    assert export(config) == 0
    path = next((config.state_dir / 'exports').glob('*.csv'))
    args = cli.build_parser().parse_args(['confirm-export', str(path)])
    assert args.func(config, args) == 0
    # Confirmation is idempotent and preserves the ledger.
    assert args.func(config, args) == 0
    result = pipeline.sync(config)
    assert result.ok and result.posted == 0 and result.skipped == 1
    remotes[1].post.assert_not_called()


def test_confirmation_preserves_existing_remote_id(config, remotes):
    assert export(config) == 0
    path = next((config.state_dir / 'exports').glob('*.csv'))
    assert pipeline.sync(config).posted == 1
    with Store(config.db_path) as store, store.exclusive():
        assert store.confirm_export(path) == 0
        assert store.conn.execute('SELECT monarch_txn_id FROM posted').fetchone()[0] == 'remote-id'


def test_missing_export_can_be_generated_again(config, remotes):
    assert export(config) == 0
    path = next((config.state_dir / 'exports').glob('*.csv'))
    path.unlink()
    assert export(config) == 0
    assert len(list(path.parent.glob('*.csv'))) == 1


def test_pending_write_blocks_export(config, remotes):
    remotes[1].post.side_effect = TimeoutError('response lost')
    assert not pipeline.sync(config).ok
    with pytest.raises(RuntimeError, match='uncertain Monarch writes'):
        export(config)


def test_recovery_cli_releases_only_requested_write(config, remotes, txn):
    remotes[1].post.side_effect = TimeoutError('response lost')
    assert not pipeline.sync(config).ok
    args = cli.build_parser().parse_args(['resolve-write', txn.dedupe_key(), '--retry'])
    assert args.func(config, args) == 0
    assert args.func(config, args) == 1
    with Store(config.db_path) as store:
        assert not store.pending_writes()


def test_orphaned_run_is_closed_on_next_sync(config, remotes):
    with Store(config.db_path) as store, store.exclusive():
        old_id = store.start_run()
    assert pipeline.sync(config).ok
    with Store(config.db_path) as store:
        old = next(row for row in store.recent_runs() if row['id'] == old_id)
        assert old['status'] == 'error' and old['finished_at']


def test_export_deduplicates_repeated_reference(config, remotes, txn):
    import csv

    remotes[0].transactions.return_value = [txn, txn]
    assert export(config) == 0
    path = next((config.state_dir / 'exports').glob('*.csv'))
    with path.open() as handle:
        assert len(list(csv.DictReader(handle))) == 1


def test_missing_wise_token_fails_export(config, remotes):
    assert export(replace(config, wise_token='')) == 1
