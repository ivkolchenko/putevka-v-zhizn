from html import escape
from urllib.parse import urlencode

from django.contrib.auth import get_user_model
from django.template.loader import render_to_string
from django.test import TestCase
from django.urls import reverse

from ..models import QuestionBlock, QuestionsBuilder, SurveyAnswer, SurveyAssignment, SurveyResponse, SurveysBuilder


class AllSurveysTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        User = get_user_model()
        cls.staff = User.objects.create_user(username='list-manager', is_staff=True)
        cls.author = User.objects.create_user(username='list-author', is_staff=True)
        cls.student = User.objects.create_user(username='list-student')
        cls.other = User.objects.create_user(username='list-other')
        cls.draft = SurveysBuilder.objects.create(title='Черновик занятий', author=cls.author)
        cls.published = SurveysBuilder.objects.create(title='Обратная связь', description='Занятия по математике',
            status=SurveysBuilder.Status.PUBLISHED, author=cls.staff)
        cls.closed = SurveysBuilder.objects.create(title='Завершённый опрос', status=SurveysBuilder.Status.CLOSED)
        cls.archived = SurveysBuilder.objects.create(title='Архивный опрос', status=SurveysBuilder.Status.ARCHIVED,
            author=cls.author)
        assignment = SurveyAssignment.objects.create(survey=cls.published, user=cls.student)
        SurveyResponse.objects.create(assignment=assignment, status=SurveyResponse.Status.SUBMITTED)
        draft_assignment = SurveyAssignment.objects.create(survey=cls.published, user=cls.other)
        response = SurveyResponse.objects.create(assignment=draft_assignment)
        question = QuestionsBuilder.objects.create(survey=cls.published, block=QuestionBlock.objects.create(survey=cls.published, title='Основной блок'), title='Частный вопрос')
        SurveyAnswer.objects.create(response=response, question=question, value='Секретный черновик ученика')

    def setUp(self):
        self.client.force_login(self.staff)
        self.url = reverse('staff_surveys_list')

    def test_all_statuses_and_authors_are_visible_to_staff(self):
        response = self.client.get(self.url)
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.context['total_count'], 4)
        self.assertEqual(set(response.context['page_obj']), {self.draft, self.published, self.closed, self.archived})
        self.assertContains(response, 'Все опросы')
        self.assertContains(response, self.author.username)
        self.assertContains(response, reverse('surveys_builder:results', args=[self.archived.pk]))
        self.assertContains(response, reverse('surveys_builder:survey_detail', args=[self.draft.pk]))

    def test_counts_use_assignments_and_final_responses_only(self):
        response = self.client.get(self.url)
        survey = next(item for item in response.context['page_obj'] if item.pk == self.published.pk)
        self.assertEqual(survey.recipient_count, 2)
        self.assertEqual(survey.submitted_count, 1)
        self.assertNotContains(response, 'Секретный черновик ученика')

    def test_search_and_status_work_together_and_reset_restores_all(self):
        response = self.client.get(self.url, {'q': 'математике занятия', 'status': 'published'})
        self.assertEqual(list(response.context['page_obj']), [self.published])
        response = self.client.get(self.url, {'q': 'занятий', 'status': 'archived'})
        self.assertEqual(response.context['page_obj'].paginator.count, 0)
        self.assertContains(response, 'По выбранным условиям опросы не найдены')
        self.assertEqual(self.client.get(self.url).context['page_obj'].paginator.count, 4)

    def test_invalid_status_is_reported_without_broadening_results(self):
        response = self.client.get(self.url, {'status': 'bad'})
        self.assertEqual(response.status_code, 200)
        self.assertIn('status', response.context['filter_form'].errors)
        self.assertEqual(response.context['page_obj'].paginator.count, 0)

    def test_search_ignores_cyrillic_case_and_treats_symbols_literally(self):
        response = self.client.get(self.url, {'q': 'ОБРАТНАЯ СВЯЗЬ'})
        self.assertEqual(list(response.context['page_obj']), [self.published])
        literal = SurveysBuilder.objects.create(title='Опрос [A+B]')
        response = self.client.get(self.url, {'q': '[A+B]'})
        self.assertEqual(list(response.context['page_obj']), [literal])
        response = self.client.get(self.url, {'q': '.*'})
        self.assertEqual(response.context['page_obj'].paginator.count, 0)

    def test_pagination_preserves_search_and_status(self):
        SurveysBuilder.objects.bulk_create([SurveysBuilder(title=f'Пакет {number}') for number in range(26)])
        query = {'q': 'Пакет', 'status': 'draft'}
        response = self.client.get(self.url, query)
        self.assertEqual(len(response.context['page_obj']), 25)
        self.assertEqual(response.context['page_obj'].paginator.count, 26)
        self.assertContains(response, '?' + escape(urlencode(query)) + '&amp;page=2')
        response = self.client.get(self.url, {**query, 'page': 2})
        self.assertEqual(len(response.context['page_obj']), 1)
        self.assertEqual(response.context['filter_form'].cleaned_data, query)

    def test_nonstaff_and_anonymous_users_cannot_list_surveys(self):
        self.client.force_login(self.student)
        self.assertEqual(self.client.get(self.url).status_code, 302)
        self.client.logout()
        self.assertEqual(self.client.get(self.url).status_code, 302)
        sidebar = render_to_string('core/_sidebar.html', {'user': self.student})
        self.assertNotIn(self.url, sidebar)

    def test_section_is_linked_from_staff_pages_and_constructor(self):
        response = self.client.get(reverse('staff_users_list'))
        self.assertContains(response, self.url)
        response = self.client.get(reverse('surveys_builder:constructor'))
        self.assertContains(response, self.url)
        sidebar = render_to_string('core/_sidebar.html', {'user': self.staff})
        self.assertIn(self.url, sidebar)

    def test_list_is_read_only_and_deleting_draft_returns_to_list(self):
        self.assertEqual(self.client.post(self.url, {'title': 'Не создавать'}).status_code, 405)
        self.assertEqual(SurveysBuilder.objects.count(), 4)
        response = self.client.post(reverse('surveys_builder:survey_delete', args=[self.draft.pk]),
            {'revision': self.draft.revision})
        self.assertRedirects(response, self.url)

    def test_empty_state_has_create_action(self):
        SurveyAnswer.objects.all().delete()
        SurveyResponse.objects.all().delete()
        SurveyAssignment.objects.all().delete()
        SurveysBuilder.objects.all().delete()
        response = self.client.get(self.url)
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, 'Опросов пока нет')
        self.assertContains(response, reverse('surveys_builder:constructor'))
