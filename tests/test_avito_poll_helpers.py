import unittest
from datetime import date

import avito_poll


class AvitoPollHelpersTests(unittest.TestCase):
    def setUp(self) -> None:
        self._last_handled_backup = dict(avito_poll._last_handled)
        self._recovery_backup = avito_poll.RECOVERY_PENDING_LIMIT

    def tearDown(self) -> None:
        avito_poll._last_handled.clear()
        avito_poll._last_handled.update(self._last_handled_backup)
        avito_poll.RECOVERY_PENDING_LIMIT = self._recovery_backup

    def test_is_client_incoming_case_insensitive(self) -> None:
        self.assertTrue(avito_poll._is_client_incoming({"direction": "in"}))
        self.assertTrue(avito_poll._is_client_incoming({"direction": "IN"}))
        self.assertFalse(avito_poll._is_client_incoming({"direction": "out"}))

    def test_pending_messages_after_known_last_handled(self) -> None:
        chat_id = "chat-1"
        avito_poll._last_handled[chat_id] = "m2"
        incoming = [
            {"id": "m1", "created": 100},
            {"id": "m2", "created": 200},
            {"id": "m3", "created": 300},
            {"id": "m4", "created": 400},
        ]
        pending = avito_poll._pending_incoming_messages(chat_id, incoming)
        self.assertEqual([m["id"] for m in pending], ["m3", "m4"])

    def test_pending_messages_gap_recovers_last_n(self) -> None:
        chat_id = "chat-2"
        avito_poll._last_handled[chat_id] = "old-missing"
        avito_poll.RECOVERY_PENDING_LIMIT = 2
        incoming = [
            {"id": "m1", "created": 100},
            {"id": "m2", "created": 200},
            {"id": "m3", "created": 300},
        ]
        pending = avito_poll._pending_incoming_messages(chat_id, incoming)
        self.assertEqual([m["id"] for m in pending], ["m2", "m3"])

    def test_decline_short_acknowledgement(self) -> None:
        self.assertTrue(avito_poll._is_declining("спасибо"))
        self.assertTrue(avito_poll._is_declining("нет"))

    def test_decline_with_question_or_date_is_not_final_refusal(self) -> None:
        self.assertFalse(avito_poll._is_declining("нет, а есть 20.06?"))
        self.assertFalse(avito_poll._is_declining("спасибо, а на 15 июля свободно?"))
        self.assertFalse(avito_poll._is_declining("не подходит 12.08, а 13.08?"))

    def test_parse_all_dates_extracts_multiple_dates(self) -> None:
        dates = avito_poll._parse_all_dates("Подойдут 15.07 и 16 июля")
        self.assertGreaterEqual(len(dates), 2)
        self.assertEqual(dates[0].day, 15)
        self.assertEqual(dates[1].day, 16)
        self.assertTrue(all(isinstance(d, date) for d in dates))


if __name__ == "__main__":
    unittest.main()
