"""Cheap signed authorization runs before persisting large private requests."""
import unittest
import app
from cloudflare.event_policy import preflight_mutation


class EventPolicyTests(unittest.TestCase):
    def setUp(self):
        self.config = {'secret': '01' * 32}

    def envelope(self, role='staff', path='/api/rewards/draft'):
        token, profile = app.sign_session(role, self.config)
        return {'method': 'POST', 'path': path, 'headers': [
            ['Cookie', 'party_session=' + token], ['X-CSRF-Token', profile['csrf']],
            ['Origin', 'https://eventdesk.example']]}

    def test_anonymous_or_forged_identity_cannot_write_an_intent(self):
        for headers in ([], [['Cookie', 'party_session=forged'], ['X-CSRF-Token', 'forged']]):
            result = preflight_mutation({'method': 'POST', 'path': '/api/rewards/draft', 'headers': headers},
                                       self.config, 'https://eventdesk.example', app.parse_session)
            self.assertEqual(result[0], 401)

    def test_csrf_and_origin_rejections_precede_tracking(self):
        value = self.envelope()
        value['headers'] = [[name, 'wrong' if name == 'X-CSRF-Token' else text] for name, text in value['headers']]
        self.assertEqual(preflight_mutation(value, self.config, 'https://eventdesk.example', app.parse_session)[0], 403)
        value = self.envelope()
        value['headers'] = [[name, 'https://untrusted.invalid' if name == 'Origin' else text] for name, text in value['headers']]
        self.assertEqual(preflight_mutation(value, self.config, 'https://eventdesk.example', app.parse_session)[0], 403)

    def test_role_guards_match_the_existing_app_including_legacy_staff(self):
        for role in ('admin', 'staff', 'lucky_draw_operator'):
            self.assertIsNone(preflight_mutation(self.envelope(role), self.config, 'https://eventdesk.example', app.parse_session))
        self.assertEqual(preflight_mutation(self.envelope('checkin_staff'), self.config,
                                           'https://eventdesk.example', app.parse_session)[0], 403)
        self.assertEqual(preflight_mutation(self.envelope('staff', '/api/admin/employees'), self.config,
                                           'https://eventdesk.example', app.parse_session)[0], 403)

    def test_reward_reorder_uses_draw_permissions(self):
        for role in ('admin', 'staff', 'lucky_draw_operator'):
            self.assertIsNone(preflight_mutation(self.envelope(role, '/api/rewards/reorder'),
                                                 self.config, 'https://eventdesk.example', app.parse_session))
        self.assertEqual(preflight_mutation(self.envelope('checkin_staff', '/api/rewards/reorder'),
                                           self.config, 'https://eventdesk.example', app.parse_session)[0], 403)

    def test_single_winner_removal_uses_draw_permissions(self):
        path = '/api/winners/remove'
        for role in ('admin', 'staff', 'lucky_draw_operator'):
            self.assertIsNone(preflight_mutation(self.envelope(role, path),
                                                 self.config, 'https://eventdesk.example', app.parse_session))
        self.assertEqual(preflight_mutation(self.envelope('checkin_staff', path),
                                           self.config, 'https://eventdesk.example', app.parse_session)[0], 403)

    def test_bulk_winner_history_removal_requires_admin(self):
        for path in ('/api/winners/history/clear', '/api/admin/draw-history/remove'):
            with self.subTest(path=path):
                self.assertIsNone(preflight_mutation(self.envelope('admin', path),
                                                     self.config, 'https://eventdesk.example', app.parse_session))
                for role in ('staff', 'lucky_draw_operator', 'checkin_staff'):
                    result = preflight_mutation(self.envelope(role, path),
                                                self.config, 'https://eventdesk.example', app.parse_session)
                    self.assertEqual(result, (403, 'Admin access required.'), role)

    def test_public_session_and_login_keep_the_original_access_policy(self):
        for path in ('/api/staff-session', '/api/login'):
            self.assertIsNone(preflight_mutation({'method': 'POST', 'path': path, 'headers': []},
                                                self.config, 'https://eventdesk.example', app.parse_session))

    def test_unknown_mutation_and_invalid_server_key_fail_closed(self):
        self.assertEqual(preflight_mutation(self.envelope(path='/api/not-a-route'), self.config,
                                           'https://eventdesk.example', app.parse_session)[0], 404)
        self.assertEqual(preflight_mutation(self.envelope(), {}, 'https://eventdesk.example', app.parse_session)[0], 503)


if __name__ == '__main__':
    unittest.main()
