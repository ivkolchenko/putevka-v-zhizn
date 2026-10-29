import io
import json
import tempfile
from datetime import timedelta

from PIL import Image
from django.contrib.auth import get_user_model
from django.core.exceptions import ValidationError
from django.core.files.uploadedfile import SimpleUploadedFile
from django.db import IntegrityError, transaction
from django.test import Client, TestCase, override_settings
from django.urls import reverse
from django.utils import timezone

from core.models import RegistrationPersonalData
from scholar_form.models import UserInfo
from . import services
from .forms import AudienceForm, BlockVisibilityForm
from .models import BlockTextSection, QuestionBlock, QuestionsBuilder, SurveyResponse, SurveysBuilder
from .selectors import assignment_questions, resolve_audience


class ReworkedSurveyTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        User = get_user_model()
        cls.staff = User.objects.create_user(username='rework-staff', is_staff=True)
        cls.it = User.objects.create_user(username='rework-it')
        cls.medic = User.objects.create_user(username='rework-medic')
        cls.inactive = User.objects.create_user(username='rework-disabled', is_active=False)
        cls.unfinished = User.objects.create_user(username='rework-unfinished')
        for user in (cls.it, cls.medic, cls.inactive):
            RegistrationPersonalData.objects.create(user=user, email=user.username+'@example.test', current_step='finish')
        RegistrationPersonalData.objects.create(user=cls.unfinished, email='unfinished@example.test', current_step='initial_data')
        UserInfo.objects.create(user=cls.it, internal_study_profile='it', next_year_class_digit=11)
        UserInfo.objects.create(user=cls.medic, internal_study_profile='medic', next_year_class_digit=10)

    def setUp(self):
        self.client.force_login(self.staff)
        self.survey = services.create_survey(self.staff, {'title': 'Новый сценарий'})
        self.media = tempfile.TemporaryDirectory()
        self.addCleanup(self.media.cleanup)
        media_override = override_settings(MEDIA_ROOT=self.media.name, SURVEY_PRIVATE_MEDIA_ROOT=self.media.name + '/private')
        media_override.enable()
        self.addCleanup(media_override.disable)

    def version(self):
        self.survey.refresh_from_db()
        return self.survey.revision

    def block(self, title='Блок', filters=None):
        return services.save_block(self.survey.pk, self.staff, {'title': title,
            'visibility_filters': filters or {}}, self.version())

    def question(self, block, title='Вопрос', required=True):
        return services.save_question(self.survey.pk, self.staff, {'block': block.pk,
            'title': title, 'answer_type': 'short_text', 'required': required}, self.version())

    def section(self, block, text='Текстовая инструкция'):
        return services.save_text_section(self.survey.pk, block.pk, self.staff, {'text': text}, {}, self.version())

    def assign(self, **extra):
        services.save_audience(self.survey.pk, self.staff, {'mode': 'filters', **extra}, self.version())
        self.version()

    def publish(self):
        self.version()
        ids = list(services.audience_for(self.survey).values_list('pk', flat=True))
        services.publish_survey(self.survey.pk, self.staff, services.publish_token(self.survey, self.staff, ids))
        return self.survey.assignments.get(user=self.it)

    def api(self, method, path, data):
        return getattr(self.client, method)(path, data=json.dumps(data), content_type='application/json')

    def png(self):
        image = io.BytesIO()
        Image.new('RGB', (4, 4), 'blue').save(image, format='PNG')
        return SimpleUploadedFile('sample.png', image.getvalue(), content_type='image/png')

    def test_create_goes_to_blocks_and_deadline_only_appears_at_assignment(self):
        response = self.client.post(reverse('surveys_builder:constructor'), {'title': 'С нуля'})
        created = SurveysBuilder.objects.get(title='С нуля')
        self.assertRedirects(response, reverse('surveys_builder:block_create', args=[created.pk]))
        self.assertNotContains(self.client.get(reverse('surveys_builder:constructor')), 'name="deadline"')
        self.assertNotContains(self.client.get(reverse('surveys_builder:survey_edit', args=[created.pk])), 'name="deadline"')
        response = self.client.get(reverse('surveys_builder:audience', args=[created.pk]))
        self.assertContains(response, 'name="deadline"')
        self.assertFalse(response.context['form'].fields['deadline'].required)

    def test_questions_require_a_block_but_do_not_require_audience(self):
        with self.assertRaises(ValidationError):
            services.save_question(self.survey.pk, self.staff, {'title': 'Нет блока'}, self.version())
        block = self.block()
        question = self.question(block)
        self.assertEqual(question.block_id, block.pk)
        self.assertFalse(self.survey.audience_configured)
        with self.assertRaises(IntegrityError), transaction.atomic():
            QuestionsBuilder.objects.create(survey=self.survey, title='Без блока')

    def test_eligibility_is_mandatory_for_filters_and_individuals(self):
        form = AudienceForm({'mode': 'filters', 'account_active': 'no', 'registration_complete': 'no'})
        self.assertTrue(form.is_valid(), form.errors)
        self.assertEqual(set(resolve_audience(form.cleaned_data)), {self.it, self.medic})
        for user in (self.inactive, self.unfinished, self.staff):
            self.assertFalse(AudienceForm({'mode': 'individual', 'included_users': [user.pk]}).is_valid())
        self.assertFalse(AudienceForm({'mode': 'filters', 'included_users': [self.it.pk]}).is_valid())

    def test_filter_contract_and_dynamic_panels(self):
        form = AudienceForm()
        self.assertEqual(list(form.fields['grades'].choices), [('10', '10 класс'), ('11', '11 класс')])
        for removed in ('form_statuses', 'account_active', 'registration_complete', 'region', 'q', 'excluded_users'):
            self.assertNotIn(removed, form.fields)
        self.assertFalse(AudienceForm({'mode': 'filters', 'grades': ['9']}).is_valid())
        self.assertEqual(BlockVisibilityForm.FILTER_FIELDS, AudienceForm.FILTER_FIELDS)
        self.assertEqual(set(BlockVisibilityForm.FILTER_FIELDS),
            {'grades', 'profiles', 'statuses', 'subjects', 'planned_exams', 'schools', 'courses', 'steps'})
        response = self.client.get(reverse('surveys_builder:audience', args=[self.survey.pk]))
        self.assertContains(response, 'Добавить фильтр')
        self.assertContains(response, 'data-filter-panel="grades" hidden')
        self.assertNotContains(response, 'name="account_active"')
        form = AudienceForm({'mode': 'filters', 'grades': ['11']})
        self.assertTrue(form.is_valid())
        self.assertEqual([group['name'] for group in form.filter_groups if group['active']], ['grades'])

    def test_deadline_is_optional_can_be_set_and_cleared_at_final_step(self):
        self.section(self.block())
        self.assign(deadline=(timezone.now() + timedelta(days=2)).isoformat())
        self.assertIsNotNone(self.survey.deadline)
        self.assign(deadline='')
        self.assertIsNone(self.survey.deadline)
        self.assertEqual(services.publication_errors(self.survey), [])
        with self.assertRaises(services.InvalidForm):
            self.assign(deadline=(timezone.now()-timedelta(days=1)).isoformat())

    def test_text_and_image_only_blocks_can_be_published_and_answered(self):
        block = self.block()
        section = services.save_text_section(self.survey.pk, block.pk, self.staff,
            {'title': 'Инструкция', 'image_alt': 'Синяя картинка'}, {'image': self.png()}, self.version())
        self.assertTrue(section.image)
        self.assertFalse(self.survey.questions.exists())
        self.assign()
        assignment = self.publish()
        response = services.save_response(assignment.pk, self.it, {}, final=True, revision=0)
        self.assertEqual(response.status, 'submitted')
        self.client.force_login(self.it)
        html = self.client.get(reverse('student_surveys:answer', args=[assignment.pk]))
        self.assertContains(html, 'Синяя картинка')

    def test_empty_section_and_invalid_images_are_rejected(self):
        block = self.block()
        with self.assertRaises(services.InvalidForm):
            self.section(block, text='')
        fake = SimpleUploadedFile('evil.svg', b'<svg onload="alert(1)"/>', content_type='image/svg+xml')
        with self.assertRaises(services.InvalidForm):
            services.save_text_section(self.survey.pk, block.pk, self.staff, {}, {'image': fake}, self.version())
        self.assertFalse(BlockTextSection.objects.exists())

    def test_order_can_mix_questions_text_and_images(self):
        block = self.block()
        first = self.section(block, 'Вступление')
        question = self.question(block)
        last = self.section(block, 'Завершение')
        from .selectors import block_items
        self.assertEqual([item['type'] for item in block_items(block)], ['section', 'question', 'section'])
        services.text_section_action(self.survey.pk, block.pk, last.pk, self.staff, 'up', self.version())
        self.assertEqual([item['id'] for item in block_items(block) if item['type'] == 'section'], [first.pk, last.pk])
        question.refresh_from_db()
        self.assertEqual(question.block_position, 3)

    def test_hidden_blocks_and_required_questions_are_neither_exposed_nor_required(self):
        common = self.block('Общий блок')
        visible = self.question(common, 'Общий вопрос')
        private = self.block('Только медицина', {'profiles': ['medic']})
        hidden = self.question(private, 'Скрытый обязательный вопрос')
        self.section(private, 'Секретный текст для медиков')
        self.assign()
        assignment = self.publish()
        self.assertEqual(assignment.visible_block_ids, [common.pk])
        self.assertEqual(list(assignment_questions(assignment)), [visible])
        self.client.force_login(self.it)
        html = self.client.get(reverse('student_surveys:answer', args=[assignment.pk]))
        self.assertNotContains(html, private.title)
        self.assertNotContains(html, 'Секретный текст')
        endpoint = reverse('student_surveys:student_surveys_api:detail', args=[assignment.pk])
        data = self.client.get(endpoint).json()
        self.assertEqual([b['id'] for b in data['survey']['blocks']], [common.pk])
        self.assertEqual([q['id'] for q in data['survey']['questions']], [visible.pk])
        with self.assertRaises(services.InvalidForm):
            services.save_response(assignment.pk, self.it, {f'q_{hidden.pk}': 'Подмена'}, final=False, revision=0)
        response = services.save_response(assignment.pk, self.it, {f'q_{visible.pk}': 'Виден'}, final=True, revision=0)
        self.assertEqual(response.answers.count(), 1)

    def test_block_new_filters_save_restore_and_clear_in_editor(self):
        response = self.client.get(reverse('surveys_builder:block_create', args=[self.survey.pk]))
        for name in ('grades', 'statuses', 'steps'):
            self.assertContains(response, f'data-filter-panel="{name}" hidden')
        response = self.client.post(reverse('surveys_builder:block_create', args=[self.survey.pk]),
            {'revision': self.version(), 'title': 'Для старшеклассников', 'grades': ['11'],
             'statuses': ['CANDIDATE'], 'steps': ['form']})
        self.assertEqual(response.status_code, 302)
        block = self.survey.blocks.get()
        url = reverse('surveys_builder:block_edit', args=[self.survey.pk, block.pk])
        response = self.client.get(url)
        form = response.context['visibility_form']
        self.assertEqual({g['name'] for g in form.filter_groups if g['active']}, {'grades', 'statuses', 'steps'})
        self.assertEqual(form['grades'].value(), ['11'])
        self.assertEqual(form['statuses'].value(), ['CANDIDATE'])
        self.assertEqual(form['steps'].value(), ['form'])
        response = self.client.post(url, {'revision': self.version(), 'title': block.title})
        self.assertEqual(response.status_code, 302)
        block.refresh_from_db()
        for name in ('grades', 'statuses', 'steps'):
            self.assertEqual(block.visibility_filters[name], [])
        for name, value in (('grades', '9'), ('statuses', 'unknown'), ('steps', 'unknown')):
            with self.subTest(name=name), self.assertRaises(services.InvalidForm):
                self.block(filters={name: [value]})

    def test_grade_status_and_step_restrict_blocks_and_are_frozen_on_publication(self):
        UserInfo.objects.filter(user=self.medic).update(status='SCHOLAR', selection_step='test')
        common = self.block('Всем')
        self.section(common)
        filters = {'grades': ['11'], 'statuses': ['CANDIDATE'], 'steps': ['form']}
        blocks = []
        for name, value in [*filters.items(), ('combined', filters)]:
            block = self.block(name, value if name == 'combined' else {name: value})
            self.section(block)
            blocks.append(block)
        self.assign()
        assignment = self.publish()
        expected = [common.pk, *(b.pk for b in blocks)]
        self.assertEqual(assignment.visible_block_ids, expected)
        self.assertEqual(self.survey.assignments.get(user=self.medic).visible_block_ids, [common.pk])
        UserInfo.objects.filter(user=self.it).update(next_year_class_digit=10, status='SCHOLAR', selection_step='test')
        assignment.refresh_from_db()
        self.assertEqual(assignment.visible_block_ids, expected)

    def test_api_accepts_new_visibility_filters_and_retains_them_on_partial_update(self):
        filters = {'grades': ['10'], 'statuses': ['CANDIDATE'], 'steps': ['form']}
        endpoint = reverse('surveys_builder:surveys_api:blocks', args=[self.survey.pk])
        response = self.api('post', endpoint, {'revision': self.version(), 'title': 'Блок API', 'visibility_filters': filters})
        self.assertEqual(response.status_code, 201, response.content)
        block = self.survey.blocks.get()
        endpoint = reverse('surveys_builder:surveys_api:block_detail', args=[self.survey.pk, block.pk])
        response = self.api('patch', endpoint, {'revision': self.version(), 'title': 'Новое название'})
        self.assertEqual(response.status_code, 200, response.content)
        for name, value in filters.items():
            self.assertEqual(response.json()['block']['visibility_filters'][name], value)
        response = self.api('patch', endpoint, {'revision': self.version(), 'visibility_filters': {}})
        self.assertEqual(response.status_code, 200, response.content)
        for name in filters:
            self.assertEqual(response.json()['block']['visibility_filters'][name], [])

    def test_visibility_snapshot_does_not_change_with_profile_after_publication(self):
        self.section(self.block('Общий'))
        medical = self.block('Медицина', {'profiles': ['medic']})
        self.section(medical)
        self.assign()
        assignment = self.publish()
        old = assignment.visible_block_ids.copy()
        UserInfo.objects.filter(user=self.it).update(internal_study_profile='medic')
        assignment.refresh_from_db()
        self.assertEqual(assignment.visible_block_ids, old)
        self.assertNotIn(medical.pk, assignment.visible_block_ids)

    def test_preview_token_rejects_changed_block_visibility(self):
        self.section(self.block('Общий'))
        block = self.block('Медицина', {'profiles': ['medic']})
        self.section(block)
        self.assign()
        ids = list(services.audience_for(self.survey).values_list('pk', flat=True))
        token = services.publish_token(self.survey, self.staff, ids)
        UserInfo.objects.filter(user=self.it).update(internal_study_profile='medic')
        with self.assertRaises(ValidationError):
            services.publish_survey(self.survey.pk, self.staff, token)
        self.assertFalse(self.survey.assignments.exists())

    def test_content_only_preview_has_no_answer_controls_or_post_action(self):
        block = self.block()
        self.question(block)
        self.section(block)
        url = reverse('surveys_builder:preview', args=[self.survey.pk])
        response = self.client.get(url)
        self.assertNotContains(response, 'name="q_')
        self.assertNotContains(response, '<textarea')
        self.assertNotContains(response, '<input type="radio"')
        self.assertNotContains(response, 'Проверить заполнение')
        self.assertEqual(self.client.post(url, {}).status_code, 405)
        self.assertFalse(SurveyResponse.objects.exists())

    def test_standalone_template_edit_import_copy_and_delete_are_independent(self):
        template = services.create_block_template(self.staff, {'title': 'Переиспользуемый блок'})
        block = template.blocks.get()
        q = services.save_question(template.pk, self.staff, {'block': block.pk, 'title': 'Из библиотеки', 'answer_type': 'short_text'}, template.revision)
        template.refresh_from_db()
        section = services.save_text_section(template.pk, block.pk, self.staff, {'text': 'Общая инструкция'}, {'image': self.png()}, template.revision)
        imported = services.import_block_template(self.survey.pk, self.staff, template.pk, self.version())
        self.assertNotEqual(imported.pk, block.pk)
        self.assertNotEqual(imported.questions.get().pk, q.pk)
        self.assertEqual(imported.text_sections.get().image.name, section.image.name)
        block.title = 'Поменяли исходник'
        block.save()
        self.assertEqual(imported.title, 'Переиспользуемый блок')
        template.refresh_from_db()
        services.delete_survey(template.pk, self.staff, template.revision)
        self.assertTrue(self.survey.blocks.filter(pk=imported.pk).exists())
        self.assertTrue(imported.text_sections.get().image.storage.exists(section.image.name))
        listing = self.client.get(reverse('staff_surveys_list'))
        self.assertNotContains(listing, 'Поменяли исходник')

    def test_library_and_content_html_flow(self):
        response = self.client.post(reverse('surveys_builder:block_templates'), {'title': 'Черновик из формы'})
        self.assertEqual(response.status_code, 302)
        template = SurveysBuilder.objects.get(is_block_template=True)
        self.assertContains(self.client.get(response.url), 'Добавить вопрос в этот блок')
        block = template.blocks.get()
        response = self.client.post(reverse('surveys_builder:block_content', args=[template.pk, block.pk]),
            {'title': 'Вопрос', 'answer_type': 'short_text', 'revision': template.revision})
        self.assertEqual(response.status_code, 302)
        template.refresh_from_db()
        upload_url = reverse('surveys_builder:text_section_create', args=[template.pk, block.pk])
        response = self.client.post(upload_url, {'revision': template.revision, 'text': 'Инструкция', 'image': self.png()})
        self.assertEqual(response.status_code, 302)
        response = self.client.post(reverse('surveys_builder:import_block_template', args=[self.survey.pk]),
            {'revision': self.version(), 'template_id': template.pk})
        self.assertEqual(response.status_code, 302)
        self.assertContains(self.client.get(response.url), 'Инструкция')

    def test_api_text_section_upload_template_and_assignment(self):
        block = self.block()
        endpoint = reverse('surveys_builder:surveys_api:text_sections', args=[self.survey.pk, block.pk])
        response = self.client.post(endpoint, {'revision': self.version(), 'text': 'Текст', 'image': self.png()})
        self.assertEqual(response.status_code, 201, response.content)
        self.assertTrue(response.json()['section']['image_url'])
        endpoint = reverse('surveys_builder:surveys_api:save_block_template', args=[self.survey.pk, block.pk])
        response = self.api('post', endpoint, {'revision': self.version()})
        self.assertEqual(response.status_code, 201, response.content)
        template_id = response.json()['id']
        endpoint = reverse('surveys_builder:surveys_api:import_block_template', args=[self.survey.pk])
        self.assertEqual(self.api('post', endpoint, {'revision': self.version(), 'template_id': template_id}).status_code, 201)
        endpoint = reverse('surveys_builder:surveys_api:audience', args=[self.survey.pk])
        response = self.api('put', endpoint, {'revision': self.version(), 'mode': 'individual', 'included_users': [self.it.pk], 'deadline': None})
        self.assertEqual(response.status_code, 200, response.content)
        self.assertEqual(response.json()['recipients']['count'], 1)

    def test_published_content_and_unauthorized_mutations_are_protected(self):
        block = self.block()
        section = self.section(block)
        self.assign()
        self.publish()
        with self.assertRaises(ValidationError):
            services.save_text_section(self.survey.pk, block.pk, self.staff, {'text': 'Нет'}, {}, self.version(), section.pk)
        self.client.force_login(self.it)
        self.assertEqual(self.client.get(reverse('surveys_builder:block_content', args=[self.survey.pk, block.pk])).status_code, 302)
        client = Client(enforce_csrf_checks=True)
        client.force_login(self.staff)
        url = reverse('surveys_builder:text_section_create', args=[self.survey.pk, block.pk])
        self.assertEqual(client.post(url, {'revision': self.version(), 'text': 'Нет'}).status_code, 403)

    def test_private_image_access_checks_assignment_and_block_visibility(self):
        self.section(self.block('Общий'))
        hidden = self.block('Медицина', {'profiles': ['medic']})
        section = services.save_text_section(self.survey.pk, hidden.pk, self.staff,
            {}, {'image': self.png()}, self.version())
        url = reverse('surveys_builder:section_image', args=[section.pk])
        response = self.client.get(url)
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response['Content-Type'], 'image/png')
        self.assertEqual(response['Cache-Control'], 'private, no-store')
        response.close()
        self.assign()
        self.publish()
        self.client.force_login(self.it)
        self.assertEqual(self.client.get(url).status_code, 404)
        self.client.force_login(self.medic)
        response = self.client.get(url)
        self.assertEqual(response.status_code, 200)
        response.close()
        self.client.logout()
        self.assertEqual(self.client.get(url).status_code, 302)
        with self.assertRaises(ValueError):
            section.image.url

    def test_private_image_can_be_edited_and_removed_without_leaking_storage_url(self):
        block = self.block()
        section = services.save_text_section(self.survey.pk, block.pk, self.staff,
            {'text': 'Оставить текст'}, {'image': self.png()}, self.version())
        url = reverse('surveys_builder:text_section_edit', args=[self.survey.pk, block.pk, section.pk])
        self.assertEqual(self.client.get(url).status_code, 200)
        endpoint = reverse('surveys_builder:surveys_api:text_section_detail', args=[self.survey.pk, block.pk, section.pk])
        response = self.api('patch', endpoint, {'revision': self.version(), 'remove_image': True})
        self.assertEqual(response.status_code, 200, response.content)
        self.assertIsNone(response.json()['section']['image_url'])
        section.refresh_from_db()
        self.assertFalse(section.image)
        self.assertEqual(section.text, 'Оставить текст')
