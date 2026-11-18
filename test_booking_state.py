"""Tests for the verified state: time gate, evidence validators, scenario readings, final text.

    python -m unittest test_booking_state -v
"""

from __future__ import annotations

import unittest
from datetime import datetime, timezone

from booking_state import Availability, BookingState, Status, mentioned_party_sizes, mentioned_times
from test_support import FakeClock, OPENING_TEXT


def state(hour: int = 12, minute: int = 0, second: int = 0, month: int = 11, day: int = 18, with_opening: bool = True) -> BookingState:
    b = BookingState(clock=FakeClock(hour, minute, second, month=month, day=day))
    if with_opening:
        b.last_outgoing_text = OPENING_TEXT
    return b


def add(b: BookingState, text: str, mid: str = "m1") -> str:
    b.record_incoming(mid, text)
    return mid


class TimeGateTests(unittest.TestCase):
    def test_before_17_is_open(self):
        self.assertFalse(state(16, 59, 59).slot_expired())

    def test_exactly_17_00_is_blocked(self):
        self.assertTrue(state(17, 0, 0).slot_expired())

    def test_after_17_is_blocked_and_state_becomes_expired(self):
        b = state(17, 1)
        self.assertTrue(b.expire_if_needed())
        self.assertIs(b.status, Status.EXPIRED)

    def test_target_is_aware_london_17_00_on_the_start_day(self):
        b = state(12)
        t = b.target_datetime()
        self.assertEqual((t.hour, t.minute, t.date().isoformat()), (17, 0, "2026-11-18"))
        self.assertEqual(t.tzinfo.key, "Europe/London")

    def test_day_change_does_not_move_the_booking_to_a_new_day(self):
        clock = FakeClock(12, 0, day=18)
        b = BookingState(clock=clock)
        clock.now = clock.now.replace(day=19, hour=1)  # after midnight
        self.assertEqual(b.target_datetime().date().isoformat(), "2026-11-18")
        self.assertTrue(b.slot_expired())  # yesterday's 17:00 is over; this is NOT a new booking

    def test_summer_time_uses_bst_not_a_fixed_offset(self):
        # 16:30 UTC on 15 July is 17:30 BST in London: the slot has passed.
        clock = FakeClock(0, 0, month=7, day=15)
        b = BookingState(clock=clock)
        clock.now = datetime(2026, 7, 15, 16, 30, tzinfo=timezone.utc)
        self.assertTrue(b.slot_expired())
        # 16:30 UTC on 18 November is 16:30 GMT: still open.
        clock2 = FakeClock(0, 0)
        b2 = BookingState(clock=clock2)
        clock2.now = datetime(2026, 11, 18, 16, 30, tzinfo=timezone.utc)
        self.assertFalse(b2.slot_expired())

    def test_terminal_status_cannot_be_left(self):
        b = state()
        b.set_status(Status.DECLINED, "x")
        self.assertFalse(b.set_status(Status.WAITING_FOR_MANAGER))
        self.assertIs(b.status, Status.DECLINED)

    def test_duplicate_incoming_id_is_rejected(self):
        b = state()
        self.assertTrue(b.record_incoming("m1", "Hello"))
        self.assertFalse(b.record_incoming("m1", "Hello again"))


class ParsingTests(unittest.TestCase):
    def test_times(self):
        self.assertEqual(mentioned_times("5 pm today"), {(17, 0)})
        self.assertEqual(mentioned_times("we can offer 5:30 pm"), {(17, 30)})
        self.assertEqual(mentioned_times("at 17:00"), {(17, 0)})
        self.assertEqual(mentioned_times("12 am or 12 pm"), {(0, 0), (12, 0)})
        self.assertEqual(mentioned_times("no times here"), set())

    def test_party_sizes(self):
        self.assertEqual(mentioned_party_sizes("a table for two at 5 pm"), {2})
        self.assertEqual(mentioned_party_sizes("for four people"), {4})
        self.assertEqual(mentioned_party_sizes("6 guests"), {6})
        self.assertEqual(mentioned_party_sizes("table for 5 pm"), set())  # 5 is a time here, not a party


class AgreementValidatorTests(unittest.TestCase):
    """The manager's evidence must really prove agreement to two guests, 17:00 today."""

    def check(self, text: str, evidence: str, expect_ok: bool, with_opening: bool = True):
        b = state(with_opening=with_opening)
        problem = b.agreement_problem(text, evidence)
        self.assertEqual(problem is None, expect_ok, f"{text!r} -> {problem}")

    def test_clear_agreement(self):
        self.check("Yes, we have a table for two at 5 pm today.", "Yes, we have a table for two at 5 pm today.", True)

    def test_sorry_with_yes_is_agreement_not_refusal(self):
        text = "Sorry for the delay. Yes, we have a table available for two at 5 pm today. We\u2019re The Ivy."
        self.check(text, "Yes, we have a table available for two at 5 pm today.", True)

    def test_yes_we_have_a_restaurant_is_not_agreement(self):
        self.check("Yes, we do have a restaurant. How can I help?", "Yes, we do have a restaurant.", False)

    def test_bare_yes_counts_only_as_answer_to_the_slot_request(self):
        self.check("Yes, that is fine.", "Yes, that is fine.", True, with_opening=True)
        self.check("Yes, that is fine.", "Yes, that is fine.", False, with_opening=False)

    def test_bare_yes_with_a_question_is_not_agreement(self):
        self.check("Yes, fine. Which day?", "Yes, fine.", False)

    def test_refusal_inside_the_message_blocks_agreement(self):
        self.check("Yes, we do have a restaurant, but we are fully booked.", "Yes, we do have a restaurant", False)
        self.check("Yes, we have a table for two at 5 pm. Unfortunately not today.", "Yes, we have a table for two at 5 pm.", False)

    def test_other_time_blocks_agreement(self):
        self.check("Yes, we have a table for two at 6 pm today.", "Yes, we have a table for two at 6 pm today.", False)
        self.check("Yes, we can do 5 pm or 5:30 pm.", "Yes, we can do 5 pm", False)

    def test_other_party_size_blocks_agreement(self):
        self.check("Yes, we have a table for four at 5 pm today.", "Yes, we have a table for four at 5 pm today.", False)

    def test_other_day_blocks_agreement(self):
        self.check("Yes, we have a table for two at 5 pm tomorrow.", "Yes, we have a table for two at 5 pm tomorrow.", False)
        self.check("Yes, we have a table for two at 5 pm on Friday.", "Yes, we have a table for two at 5 pm on Friday.", False)

    def test_invented_evidence_is_rejected(self):
        self.check("We might have something.", "Yes, we have a table for two at 5 pm today.", False)

    def test_alternative_offer_blocks_agreement(self):
        self.check("Yes, we can offer you a table instead at 5 pm.", "Yes, we can offer you a table instead at 5 pm.", False)


class RestaurantValidatorTests(unittest.TestCase):
    TEXT = "Good afternoon. You\u2019re speaking with The Ivy, Covent Garden."

    def test_name_from_the_message_is_accepted(self):
        self.assertIsNone(BookingState.restaurant_problem(self.TEXT, "The Ivy", "You\u2019re speaking with The Ivy, Covent Garden."))

    def test_invented_name_is_rejected(self):
        self.assertIsNotNone(BookingState.restaurant_problem(self.TEXT, "Invented Bistro", "You\u2019re speaking with Invented Bistro"))

    def test_real_name_with_invented_quote_is_rejected(self):
        self.assertIsNotNone(BookingState.restaurant_problem(self.TEXT, "The Ivy", "This is The Ivy"))

    def test_name_missing_from_its_own_quote_is_rejected(self):
        self.assertIsNotNone(BookingState.restaurant_problem(self.TEXT, "Nobu", "Good afternoon."))

    def test_generic_or_empty_names_are_rejected(self):
        for name in ("", "restaurant", "The Restaurant", "   ", "1234"):
            self.assertIsNotNone(BookingState.restaurant_problem("the restaurant 1234", name, "the restaurant 1234"), name)


class ScenarioReadingTests(unittest.TestCase):
    """record_reply with the fixed manager messages from the handoff (section 8)."""

    def test_8_1_happy_path(self):
        b = state()
        text = "Good afternoon. Yes, we have a table for two at 5 pm today. You\u2019re speaking with The Ivy, Covent Garden."
        mid = add(b, text)
        r = b.record_reply(mid, "availability_confirmed", "Yes, we have a table for two at 5 pm today.",
                           "The Ivy", "You\u2019re speaking with The Ivy, Covent Garden.")
        self.assertTrue(r.agreement_verified)
        self.assertEqual(r.restaurant_verified, "The Ivy")
        self.assertEqual(b.restaurant_source_id, mid)
        self.assertEqual(b.agreement_source_id, mid)
        self.assertEqual(b.can_confirm(), (True, ""))

    def test_8_2_name_without_agreement_is_not_a_confirmation(self):
        b = state()
        mid = add(b, "We\u2019re The Ivy, Covent Garden. What time were you looking for?")
        b.record_reply(mid, "restaurant_identified", "", "The Ivy", "We\u2019re The Ivy, Covent Garden.")
        self.assertEqual(b.restaurant_name, "The Ivy")
        ok, reason = b.can_confirm()
        self.assertFalse(ok)
        self.assertIn("agreed", reason)

    def test_8_3_agreement_without_name_cannot_confirm(self):
        b = state()
        mid = add(b, "Yes, we can accommodate two guests at 5 pm today.")
        b.record_reply(mid, "availability_confirmed", "Yes, we can accommodate two guests at 5 pm today.")
        self.assertTrue(b.manager_agreed)
        ok, reason = b.can_confirm()
        self.assertFalse(ok)
        self.assertIn("restaurant name", reason)

    def test_8_5_decline_with_name(self):
        b = state()
        mid = add(b, "This is The Ivy, but unfortunately we\u2019re fully booked at 5 pm today.")
        b.record_reply(mid, "availability_declined", "unfortunately we\u2019re fully booked at 5 pm today",
                       "The Ivy", "This is The Ivy")
        self.assertIs(b.availability, Availability.UNAVAILABLE)
        self.assertFalse(b.can_confirm()[0])

    def test_8_6_alternative_time_is_not_accepted(self):
        b = state()
        mid = add(b, "We are fully booked at 5 pm, but we can offer 5:30 pm.")
        b.record_reply(mid, "alternative_offered", "we can offer 5:30 pm")
        self.assertIs(b.availability, Availability.ALTERNATIVE)
        self.assertFalse(b.can_confirm()[0])

    def test_8_7_ambiguous_yes_is_rejected_even_if_the_model_calls_it_agreement(self):
        b = state()
        mid = add(b, "Yes, we do have a restaurant. How can I help?")
        r = b.record_reply(mid, "availability_confirmed", "Yes, we do have a restaurant.")
        self.assertFalse(r.agreement_verified)
        self.assertTrue(r.problems)
        self.assertIn("clarifying", r.hint)

    def test_model_cannot_claim_agreement_on_a_decline(self):
        b = state()
        mid = add(b, "We are fully booked at 5 pm.")
        r = b.record_reply(mid, "availability_confirmed", "We are fully booked at 5 pm.")
        self.assertFalse(r.agreement_verified)

    def test_unknown_message_id_is_rejected(self):
        b = state()
        r = b.record_reply("nope", "availability_confirmed", "Yes")
        self.assertTrue(r.problems)
        self.assertFalse(b.manager_agreed)

    def test_conflicting_restaurant_name_is_not_overwritten(self):
        b = state()
        m1 = add(b, "We are The Ivy.", "m1")
        b.record_reply(m1, "restaurant_identified", "", "The Ivy", "We are The Ivy.")
        m2 = add(b, "This is Nobu.", "m2")
        r = b.record_reply(m2, "restaurant_identified", "", "Nobu", "This is Nobu.")
        self.assertEqual(b.restaurant_name, "The Ivy")
        self.assertTrue(r.problems)

    def test_later_decline_revokes_an_earlier_agreement(self):
        b = state()
        m1 = add(b, "Yes, we have a table for two at 5 pm today. We are The Ivy.", "m1")
        b.record_reply(m1, "availability_confirmed", "Yes, we have a table for two at 5 pm today.", "The Ivy", "We are The Ivy.")
        m2 = add(b, "Sorry, we are fully booked after all.", "m2")
        b.record_reply(m2, "availability_declined", "we are fully booked after all")
        self.assertFalse(b.can_confirm()[0])

    def test_guest_detail_request_sets_operator_flag_and_hint(self):
        b = state()
        mid = add(b, "Certainly. May I have the name for the reservation?")
        r = b.record_reply(mid, "requests_guest_details", "May I have the name for the reservation?")
        self.assertTrue(b.operator_input_needed)
        self.assertIn("do not invent", r.hint.lower())
        self.assertIn("not promise", r.hint.lower())

    def test_confirmation_is_refused_when_the_slot_passes_after_agreement(self):
        clock = FakeClock(16, 59, 59)
        b = BookingState(clock=clock)
        b.last_outgoing_text = OPENING_TEXT
        mid = add(b, "Yes, we have a table for two at 5 pm today. We are The Ivy.")
        b.record_reply(mid, "availability_confirmed", "Yes, we have a table for two at 5 pm today.", "The Ivy", "We are The Ivy.")
        self.assertTrue(b.can_confirm()[0])
        clock.set(17, 0, 1)
        self.assertEqual(b.can_confirm(), (False, "17:00 London time has already passed"))


class FinalTextTests(unittest.TestCase):
    def test_final_text_has_date_time_party_and_restaurant(self):
        b = state()
        b.restaurant_name = "The Ivy"
        text = b.final_confirmation_text()
        for needle in ("Wednesday", "18 November 2026", "17:00", "London", "table for two", "The Ivy"):
            self.assertIn(needle, text)

    def test_final_text_day_is_not_zero_padded(self):
        b = state(month=3, day=5)
        b.restaurant_name = "X"
        self.assertIn(" 5 March 2026", b.final_confirmation_text())


if __name__ == "__main__":
    unittest.main()
