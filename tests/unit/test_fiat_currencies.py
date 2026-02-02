import pytest

from src.data.fiat_currencies import (
    is_fiat_currency,
    is_stablecoin,
    parse_base_quote,
    has_disallowed_base,
)


class TestIsFiatCurrency:
    def test_known_fiats(self) -> None:
        assert is_fiat_currency("GBP") is True
        assert is_fiat_currency("EUR") is True
        assert is_fiat_currency("USD") is True
        assert is_fiat_currency("JPY") is True

    def test_stablecoins_not_fiat(self) -> None:
        assert is_fiat_currency("USDT") is False
        assert is_fiat_currency("USDC") is False
        assert is_fiat_currency("DAI") is False

    def test_crypto_not_fiat(self) -> None:
        assert is_fiat_currency("BTC") is False
        assert is_fiat_currency("ETH") is False
        assert is_fiat_currency("SOL") is False

    def test_kraken_prefixed_codes(self) -> None:
        assert is_fiat_currency("ZUSD") is True
        assert is_fiat_currency("ZEUR") is True


class TestIsStablecoin:
    def test_known_stablecoins(self) -> None:
        assert is_stablecoin("USDT") is True
        assert is_stablecoin("USDC") is True
        assert is_stablecoin("DAI") is True
        assert is_stablecoin("PYUSD") is True

    def test_fiats_and_crypto_not_stablecoin(self) -> None:
        assert is_stablecoin("USD") is False
        assert is_stablecoin("EUR") is False
        assert is_stablecoin("BTC") is False


class TestParseBaseQuote:
    def test_spot(self) -> None:
        assert parse_base_quote("BTC/USD") == ("BTC", "USD")
        assert parse_base_quote("GBP/USD") == ("GBP", "USD")

    def test_ccxt_unified_futures(self) -> None:
        assert parse_base_quote("BTC/USD:USD") == ("BTC", "USD")
        assert parse_base_quote("GBP/USD:USD") == ("GBP", "USD")

    def test_kraken_raw(self) -> None:
        assert parse_base_quote("PF_XBTUSD") == ("XBT", "USD")
        assert parse_base_quote("PI_GBPUSD") == ("GBP", "USD")

    def test_legacy(self) -> None:
        assert parse_base_quote("BTCUSD-PERP") == ("BTC", "USD")


class TestHasDisallowedBase:
    def test_true_for_forex_like_pairs(self) -> None:
        assert has_disallowed_base("GBP/USD") is True
        assert has_disallowed_base("EUR/USD") is True
        assert has_disallowed_base("AUD/USD") is True

    def test_true_for_stablecoin_bases(self) -> None:
        assert has_disallowed_base("USDT/USD") is True
        assert has_disallowed_base("USDC/USD") is True
        assert has_disallowed_base("DAI/USD") is True

    def test_false_for_crypto_pairs(self) -> None:
        assert has_disallowed_base("BTC/USD") is False
        assert has_disallowed_base("ETH/USD") is False

