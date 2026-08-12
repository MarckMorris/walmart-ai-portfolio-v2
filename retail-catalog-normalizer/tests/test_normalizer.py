"""Catalogue normalisation rules.

Every test here corresponds to a way a real supplier feed breaks a join.
"""

from __future__ import annotations

from decimal import Decimal

import pytest

from app.normalizer import (
    build_match_key,
    check_digit,
    clean_name,
    group_duplicates,
    normalise,
    normalise_batch,
    normalise_gtin,
    parse_pack_count,
    parse_size,
    strip_accents,
)


def key(name, **extra):
    return normalise({"name": name, **extra}).match_key


class TestSizeParsing:
    @pytest.mark.parametrize(
        "text,expected_ml",
        [
            ("2 LTR", 2000),
            ("2l", 2000),
            ("2000ML", 2000),
            ("2 litres", 2000),
            ("0.5 L", 500),
            ("1,5 l", 1500),  # comma decimal, common in European feeds
        ],
    )
    def test_volumes_convert_to_millilitres(self, text, expected_ml):
        value, unit, dimension = parse_size(text)
        assert value == Decimal(expected_ml)
        assert unit == "ml"
        assert dimension == "volume"

    @pytest.mark.parametrize(
        "text,expected_g",
        [("500g", 500), ("0.5 kg", 500), ("2 KG", 2000), ("1000 mg", 1)],
    )
    def test_weights_convert_to_grams(self, text, expected_g):
        value, unit, dimension = parse_size(text)
        assert value == Decimal(expected_g)
        assert unit == "g"
        assert dimension == "weight"

    def test_imperial_units_are_converted_not_rejected(self):
        value, unit, _ = parse_size("12 oz")
        assert unit == "g"
        assert 340 < float(value) < 341

    def test_fluid_ounces_are_a_volume_not_a_weight(self):
        """The "fl oz" alias must beat "oz", or drinks become weights."""
        value, unit, dimension = parse_size("16 fl oz")
        assert dimension == "volume"
        assert unit == "ml"
        assert 473 < float(value) < 474

    def test_a_nameless_size_returns_nothing(self):
        assert parse_size("assorted colours") == (None, None, None)


class TestPackCount:
    @pytest.mark.parametrize(
        "text,expected",
        [
            ("6 x 330ml", 6),
            ("12 pack", 12),
            ("24ct", 24),
            ("pack of 8", 8),
            ("6pk", 6),
            ("500ml", 1),
        ],
    )
    def test_multipacks_are_recognised(self, text, expected):
        assert parse_pack_count(text) == expected

    def test_a_pack_of_one_is_not_a_multipack(self):
        assert parse_pack_count("1 pack") == 1

    def test_an_implausible_count_is_ignored(self):
        """Otherwise a year or a stray size becomes a pack count."""
        assert parse_pack_count("2024 pack") == 1


class TestGtin:
    def test_a_valid_upc_is_padded_to_gtin14(self):
        gtin, warning = normalise_gtin("036000291452")
        assert gtin == "00036000291452"
        assert warning is None

    def test_the_check_digit_algorithm_matches_gs1(self):
        assert check_digit("03600029145") == 2

    def test_a_transposed_digit_is_caught(self):
        """A barcode that fails its own checksum is a transcription error, and
        accepting it silently is how two products get merged."""
        gtin, warning = normalise_gtin("036000291453")
        assert gtin is None
        assert "check digit" in warning

    def test_formatting_characters_are_stripped(self):
        assert normalise_gtin("0-36000-29145-2")[0] == "00036000291452"

    @pytest.mark.parametrize("value", ["12345", "1234567890123456"])
    def test_an_impossible_length_is_reported(self, value):
        gtin, warning = normalise_gtin(value)
        assert gtin is None
        assert "digits" in warning

    def test_a_missing_barcode_is_not_an_error(self):
        assert normalise_gtin(None) == (None, None)

    def test_a_non_numeric_barcode_is_reported(self):
        gtin, warning = normalise_gtin("N/A")
        assert gtin is None
        assert "no digits" in warning


class TestNameCleaning:
    def test_accents_are_folded(self):
        assert strip_accents("Nestl\u00e9") == "Nestle"

    def test_hyphens_and_spaces_are_equivalent(self):
        assert clean_name("Coca-Cola") == clean_name("Coca Cola")

    def test_case_is_folded(self):
        assert clean_name("COCA COLA") == clean_name("coca cola")

    def test_stop_words_are_dropped(self):
        assert "the" not in clean_name("The Best Product Ever").split()

    def test_whitespace_is_collapsed(self):
        assert clean_name("  too   much   space ") == "too much space"


class TestMatchKey:
    def test_three_spellings_of_the_same_product_agree(self):
        """This is the entire point of the service."""
        first = key("Coca-Cola 2 LTR", brand="Coca-Cola")
        second = key("coca cola 2l", brand="coca cola")
        third = key("COCA COLA 2000ML", brand="Coca Cola")
        assert first == second == third

    def test_different_products_do_not_collide(self):
        assert key("Coca Cola 2L", brand="Coca Cola") != key(
            "Coca Cola Zero 2L", brand="Coca Cola"
        )

    def test_different_sizes_do_not_collide(self):
        assert key("Coca Cola 2L", brand="Coca Cola") != key(
            "Coca Cola 500ml", brand="Coca Cola"
        )

    def test_a_multipack_is_distinct_from_a_single(self):
        assert key("Water 6 x 500ml") != key("Water 500ml")

    def test_a_repeated_brand_is_not_duplicated_in_the_key(self):
        """Suppliers routinely put the brand in both the brand and name fields."""
        assert normalise({"name": "Coca Cola 2L", "brand": "Coca Cola"}).name == ""

    def test_the_key_is_stable_across_calls(self):
        record = {"name": "Spring Water 6 x 500ml", "brand": "Acme"}
        assert normalise(record).match_key == normalise(record).match_key

    def test_size_does_not_appear_as_free_text_in_the_name(self):
        product = normalise({"name": "Coca Cola 2 LTR"})
        assert "ltr" not in product.name
        assert "2" not in product.name


class TestNormaliseRecord:
    def test_alternative_name_fields_are_accepted(self):
        for field_name in ("name", "title", "description"):
            assert normalise({field_name: "Water 500ml"}).raw_name == "Water 500ml"

    def test_alternative_barcode_fields_are_accepted(self):
        for field_name in ("gtin", "upc", "ean"):
            assert normalise({"name": "x", field_name: "036000291452"}).gtin is not None

    def test_unknown_columns_are_ignored(self):
        """Supplier feeds always carry extra columns; that is not an error."""
        product = normalise({"name": "Water 500ml", "supplier_internal_ref": "ZZ-9"})
        assert product.size_value == Decimal(500)

    def test_an_empty_record_is_reported_not_crashed(self):
        product = normalise({})
        assert "no name" in " ".join(product.warnings)

    def test_a_missing_size_is_warned_about(self):
        assert any("no size" in w for w in normalise({"name": "Mystery item"}).warnings)

    def test_the_raw_name_is_preserved_for_audit(self):
        assert normalise({"name": "  Coca-Cola 2 LTR  "}).raw_name == "Coca-Cola 2 LTR"

    def test_the_result_serialises_to_plain_json_types(self):
        payload = normalise({"name": "Water 500ml"}).to_dict()
        assert isinstance(payload["size_value"], float)
        assert isinstance(payload["warnings"], list)


class TestBatch:
    def test_every_record_produces_a_result(self):
        records = [{"name": f"Item {i} 500ml"} for i in range(10)]
        assert len(normalise_batch(records)) == 10

    def test_duplicates_are_grouped_by_key(self):
        products = normalise_batch(
            [
                {"name": "Coca-Cola 2 LTR", "brand": "Coca-Cola"},
                {"name": "coca cola 2l", "brand": "coca cola"},
                {"name": "Water 500ml"},
            ]
        )
        groups = group_duplicates(products)
        assert len(groups) == 1
        assert list(groups.values())[0] == [0, 1]

    def test_unique_records_produce_no_groups(self):
        products = normalise_batch([{"name": "A 1l"}, {"name": "B 2l"}])
        assert group_duplicates(products) == {}

    def test_empty_keys_are_never_grouped(self):
        """Two unparseable records are not evidence that they are the same product."""
        products = normalise_batch([{}, {}])
        assert group_duplicates(products) == {}


def test_build_match_key_omits_absent_parts():
    assert build_match_key("water", None, None, None, 1) == "water"
