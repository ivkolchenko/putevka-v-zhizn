from django.contrib.auth import get_user_model
from django.test import TestCase
from django.urls import reverse


class StaffEntryPointTests(TestCase):
    def test_staff_root_opens_user_list_and_preserves_filters(self):
        staff = get_user_model().objects.create_user(username="staff-entry", is_staff=True)
        self.client.force_login(staff)

        response = self.client.get(reverse("staff_index"), {"q": "Иванова"}, follow=True)

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.redirect_chain, [(reverse("staff_users_list") + "?q=%D0%98%D0%B2%D0%B0%D0%BD%D0%BE%D0%B2%D0%B0", 302)])
        self.assertEqual(response.resolver_match.url_name, "staff_users_list")
        self.assertNotContains(response, "Все опросы")
        self.assertNotContains(response, "Создать опросник")
        self.assertContains(response, "Выгрузить Excel")

    def test_anonymous_staff_root_still_requires_login(self):
        response = self.client.get(reverse("staff_index"), follow=True)

        self.assertEqual(response.redirect_chain, [
            (reverse("staff_users_list"), 302),
            (reverse("login") + "?next=" + reverse("staff_users_list"), 302),
        ])
        self.assertEqual(response.status_code, 200)

    def test_staff_entry_does_not_capture_survey_routes(self):
        staff = get_user_model().objects.create_user(username="staff-survey-entry", is_staff=True)
        self.client.force_login(staff)

        self.assertEqual(self.client.get(reverse("surveys_builder:constructor")).status_code, 200)
        self.assertEqual(self.client.get(reverse("surveys_builder:surveys_api:list")).status_code, 200)
