import csv
import io
import json
from unittest.mock import patch

from django.conf import settings
from django.contrib.auth import get_user_model
from django.core.exceptions import PermissionDenied, ValidationError
from django.db import connection
from django.db.migrations.executor import MigrationExecutor
from django.http import Http404
from django.test import Client, TestCase, TransactionTestCase
from django.urls import reverse

from core.models import RegistrationPersonalData

from . import services
from .forms import QuestionsBuilderForm
from .models import QuestionBlock, QuestionsBuilder, SurveyAnswer, SurveyResponse, SurveysBuilder


class QuestionBlockTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        User = get_user_model()
        cls.staff = User.objects.create_user(username='blocks-staff', is_staff=True)
        cls.student = User.objects.create_user(username='blocks-student')
        RegistrationPersonalData.objects.create(user=cls.student, email='student-registration@example.test', current_step='finish')

    def setUp(self):
        self.client.force_login(self.staff)
        self.survey = services.create_survey(self.staff, {'title': 'Блочный опрос'})
        self.survey = services.save_audience(self.survey.pk, self.staff,
            {'mode': 'individual', 'included_users': [self.student.pk]}, self.survey.revision)

    def version(self):
        self.survey.refresh_from_db()
        return self.survey.revision

    def block(self, title='Блок A'):
        return services.save_block(self.survey.pk, self.staff,
            {'title': title, 'description': 'Описание ' + title}, self.version())

    def question(self, title, block=None, **extra):
        return services.save_question(self.survey.pk, self.staff, {
            'title': title, 'answer_type': 'short_text', 'block': (block or self.survey.blocks.first() or self.block()).pk, **extra,
        }, self.version())

    def ids(self):
        return list(self.survey.questions.values_list('pk', flat=True))

    def api(self, method, name, data=None, *args):
        url = reverse('surveys_builder:surveys_api:' + name, args=[self.survey.pk, *args])
        if method == 'get':
            return self.client.get(url)
        return getattr(self.client, method)(url, data=json.dumps(data), content_type='application/json')

    def publish(self):
        self.version()
        services.publish_survey(self.survey.pk, self.staff,
            services.publish_token(self.survey, self.staff, [self.student.pk]))
        self.survey.refresh_from_db()
        return self.survey.assignments.get()

    def test_create_edit_and_validate_block(self):
        block = self.block()
        original = self.version()
        edited = services.save_block(self.survey.pk, self.staff,
            {'title': 'Новое название', 'description': 'Новое описание'}, original, block.pk)
        self.assertEqual(edited.title, 'Новое название')
        self.assertEqual(self.version(), original + 1)
        for title in ('', ' ', 'x' * 201):
            with self.assertRaises(services.InvalidForm):
                services.save_block(self.survey.pk, self.staff, {'title': title}, self.version())
        self.assertEqual(self.survey.blocks.count(), 1)

    def test_question_order_is_grouped_and_move_appends_to_target(self):
        a, b = self.block(), self.block('B')
        b1, a1 = self.question('B1', b), self.question('A1', a)
        self.assertEqual(self.ids(), [a1.pk, b1.pk])
        services.save_question(self.survey.pk, self.staff, {'title': a1.title, 'answer_type': a1.answer_type, 'block': b.pk}, self.version(), a1.pk)
        self.assertEqual(self.ids(), [b1.pk, a1.pk])
        with self.assertRaises(services.InvalidForm):
            services.save_question(self.survey.pk, self.staff, {'title': b1.title, 'answer_type': b1.answer_type, 'block': ''}, self.version(), b1.pk)

    def test_question_moves_stay_inside_block_and_duplicate_keeps_block_and_options(self):
        block_a, block_b = self.block(), self.block('Блок B')
        a1 = self.question('A1', block_a, answer_type='single_choice', options_text='Да\nНет')
        a2 = self.question('A2', block_a)
        b = self.question('B1', block_b)
        services.question_action(self.survey.pk, a2.pk, self.staff, 'down', self.version())
        self.assertEqual(self.ids(), [a1.pk, a2.pk, b.pk])
        services.question_action(self.survey.pk, a2.pk, self.staff, 'up', self.version())
        self.assertEqual(self.ids(), [a2.pk, a1.pk, b.pk])
        services.question_action(self.survey.pk, a1.pk, self.staff, 'duplicate', self.version())
        copied = self.survey.questions.exclude(pk__in=[a1.pk, a2.pk, b.pk]).get()
        self.assertEqual(copied.block_id, block_a.pk)
        self.assertEqual(list(copied.options.values_list('text', flat=True)), ['Да', 'Нет'])
        self.assertEqual(self.ids(), [a2.pk, a1.pk, copied.pk, b.pk])

    def test_reordering_blocks_moves_their_questions(self):
        a, b = self.block(), self.block('Блок B')
        a1, b1 = self.question('A1', a), self.question('B1', b)
        services.reorder_blocks(self.survey.pk, self.staff, [b.pk, a.pk], self.version())
        self.assertEqual(self.ids(), [b1.pk, a1.pk])
        services.block_action(self.survey.pk, a.pk, self.staff, 'up', self.version())
        self.assertEqual(self.ids(), [a1.pk, b1.pk])

    def test_bad_reorders_are_atomic_and_cannot_interleave_blocks(self):
        a, b = self.block(), self.block('Блок B')
        a1, a2, b1 = self.question('A1', a), self.question('A2', a), self.question('B1', b)
        original_revision = self.version()
        for ids in ([], [a.pk, a.pk], [a.pk, 999999]):
            with self.assertRaises(ValidationError):
                services.reorder_blocks(self.survey.pk, self.staff, ids, original_revision)
        with self.assertRaises(ValidationError):
            services.reorder_questions(self.survey.pk, self.staff, [a1.pk, b1.pk, a2.pk], original_revision)
        self.assertEqual(self.ids(), [a1.pk, a2.pk, b1.pk])
        self.assertEqual(self.version(), original_revision)
        services.reorder_questions(self.survey.pk, self.staff, [a2.pk, a1.pk, b1.pk], original_revision)
        self.assertEqual(self.ids(), [a2.pk, a1.pk, b1.pk])

    def test_deleting_block_removes_its_content_and_keeps_other_blocks(self):
        a, b = self.block(), self.block('Блок B')
        a1 = self.question('A1', a, answer_type='single_choice', options_text='Да\nНет')
        b1 = self.question('B1', b)
        services.block_action(self.survey.pk, a.pk, self.staff, 'delete', self.version())
        self.assertFalse(QuestionsBuilder.objects.filter(pk=a1.pk).exists())
        self.assertEqual(self.ids(), [b1.pk])
        b.refresh_from_db()
        self.assertEqual(b.position, 1)

    def test_blocks_and_questions_cannot_reference_another_survey(self):
        foreign_survey = SurveysBuilder.objects.create(title='Другой опрос')
        foreign = QuestionBlock.objects.create(survey=foreign_survey, title='Чужой блок')
        form = QuestionsBuilderForm({'title': 'Вопрос', 'answer_type': 'short_text', 'block': foreign.pk},
            instance=QuestionsBuilder(survey=self.survey))
        self.assertFalse(form.is_valid())
        self.assertIn('block', form.errors)
        with self.assertRaises(ValidationError):
            QuestionsBuilder(survey=self.survey, block=foreign, title='Вопрос').clean()
        with self.assertRaises(Http404):
            services.save_block(self.survey.pk, self.staff, {'title': 'Нет'}, self.version(), foreign.pk)
        with self.assertRaises(services.InvalidForm):
            self.question('Нет', foreign)

    def test_staff_rights_revision_and_publication_freeze(self):
        block = self.block()
        question = self.question('Вопрос', block)
        with self.assertRaises(PermissionDenied):
            services.save_block(self.survey.pk, self.student, {'title': 'Нет'}, self.version())
        with self.assertRaises(services.RevisionConflict):
            services.block_action(self.survey.pk, block.pk, self.staff, 'delete', 0)
        assignment = self.publish()
        response = services.save_response(assignment.pk, self.student, {f'q_{question.pk}': 'Ответ'}, final=True, revision=0)
        for action in (
            lambda: services.save_block(self.survey.pk, self.staff, {'title': 'Нет'}, self.version()),
            lambda: services.save_block(self.survey.pk, self.staff, {'title': 'Нет'}, self.version(), block.pk),
            lambda: services.block_action(self.survey.pk, block.pk, self.staff, 'delete', self.version()),
            lambda: services.reorder_blocks(self.survey.pk, self.staff, [block.pk], self.version()),
        ):
            with self.assertRaises(ValidationError):
                action()
        self.assertEqual(response.answers.get().value, 'Ответ')

    def test_block_limit_and_empty_block_publication_validation(self):
        block = self.block()
        self.assertTrue(any(block.title in error for error in services.publication_errors(self.survey)))
        self.question('Есть вопрос', block)
        self.assertEqual(services.publication_errors(self.survey), [])
        QuestionBlock.objects.bulk_create([QuestionBlock(survey=self.survey, title=f'Блок {i}', position=i + 2) for i in range(49)])
        with self.assertRaises(ValidationError):
            self.block('Лишний')

    def test_copy_keeps_block_structure_without_source_references(self):
        block = self.block()
        self.question('Варианты', block, answer_type='single_choice', options_text='Да\nНет')
        self.question('Общий')
        copy = services.copy_survey(self.survey.pk, self.staff)
        copied_block = copy.blocks.get()
        self.assertNotEqual(copied_block.pk, block.pk)
        self.assertEqual(copied_block.title, block.title)
        self.assertEqual(copy.questions.get(title='Варианты').block_id, copied_block.pk)
        self.assertEqual(copy.questions.get(title='Общий').block_id, copied_block.pk)
        self.assertFalse(copy.assignments.exists())

    def test_api_block_crud_and_question_patch_move(self):
        response = self.api('post', 'blocks', {'revision': self.version(), 'title': 'Блок API', 'description': 'Подсказка'})
        self.assertEqual(response.status_code, 201)
        block_id = response.json()['block']['id']
        response = self.api('post', 'questions', {'revision': self.version(), 'title': 'Вопрос API', 'block_id': block_id})
        self.assertEqual(response.status_code, 201)
        question_id = response.json()['question']['id']
        self.assertEqual(response.json()['question']['block_id'], block_id)
        response = self.api('patch', 'question_detail', {'revision': self.version(), 'description': 'Изменено'}, question_id)
        self.assertEqual(response.json()['question']['block_id'], block_id)
        response = self.api('patch', 'block_detail', {'revision': self.version(), 'title': 'Новое имя'}, block_id)
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()['block']['description'], 'Подсказка')
        self.assertEqual(self.api('get', 'blocks').json()['blocks'][0]['title'], 'Новое имя')
        self.assertEqual(self.api('get', 'block_detail', None, block_id).status_code, 200)
        response = self.api('get', 'detail')
        self.assertEqual(response.json()['blocks'][0]['id'], block_id)
        self.assertEqual(response.json()['questions'][0]['block_id'], block_id)
        response = self.api('delete', 'block_detail', {'revision': self.version()}, block_id)
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()['questions'], [])
        self.assertEqual(response.json()['blocks'], [])

    def test_api_reordering_and_rejecting_null_block(self):
        a, b = self.block(), self.block('B')
        a1, b1 = self.question('A1', a), self.question('B1', b)
        response = self.api('put', 'block_reorder', {'revision': self.version(), 'block_ids': [b.pk, a.pk]})
        self.assertEqual(response.status_code, 200)
        self.assertEqual([q['id'] for q in response.json()['questions']], [b1.pk, a1.pk])
        response = self.api('patch', 'question_detail', {'revision': self.version(), 'block_id': None}, a1.pk)
        self.assertEqual(response.status_code, 400)

    def test_api_rejects_malformed_ids_and_conflicts_without_mutation(self):
        a = self.block()
        q = self.question('Q', a)
        revision = self.version()
        for value in (True, '1', -1, [], {}):
            response = self.api('patch', 'question_detail', {'revision': revision, 'block_id': value}, q.pk)
            self.assertEqual(response.status_code, 400)
        for ids in ([True], [[a.pk]], [a.pk, a.pk], []):
            response = self.api('put', 'block_reorder', {'revision': revision, 'block_ids': ids})
            self.assertEqual(response.status_code, 400)
        response = self.api('patch', 'block_detail', {'revision': 0, 'title': 'Нет'}, a.pk)
        self.assertEqual(response.status_code, 409)
        self.assertEqual(self.version(), revision)

    def test_api_block_mutations_require_staff_and_csrf(self):
        block = self.block()
        url = reverse('surveys_builder:surveys_api:block_detail', args=[self.survey.pk, block.pk])
        client = Client(enforce_csrf_checks=True)
        client.force_login(self.staff)
        self.assertEqual(client.delete(url, data=json.dumps({'revision': self.version()}), content_type='application/json').status_code, 403)
        self.client.force_login(self.student)
        self.assertEqual(self.api('get', 'blocks').status_code, 403)
        self.assertEqual(self.api('post', 'blocks', {'revision': self.version(), 'title': 'Нет'}).status_code, 403)

    def test_html_create_edit_delete_and_question_selection(self):
        url = reverse('surveys_builder:block_create', args=[self.survey.pk])
        self.assertEqual(self.client.get(url).status_code, 200)
        response = self.client.post(url, {'title': 'Блок через форму', 'revision': self.version()})
        self.assertEqual(response.status_code, 302)
        block = self.survey.blocks.get()
        response = self.client.get(reverse('surveys_builder:survey_detail', args=[self.survey.pk]), {'block': block.pk})
        self.assertContains(response, block.title)
        self.assertEqual(response.context['form']['block'].value(), str(block.pk))
        question = self.question('В блоке', block)
        response = self.client.post(reverse('surveys_builder:block_edit', args=[self.survey.pk, block.pk]),
            {'title': 'Отредактирован', 'revision': self.version()})
        self.assertEqual(response.status_code, 302)
        delete_url = reverse('surveys_builder:block_delete', args=[self.survey.pk, block.pk])
        self.assertContains(self.client.get(delete_url), 'будут удалены')
        self.assertTrue(self.survey.blocks.exists())
        self.assertEqual(self.client.post(delete_url, {'revision': self.version()}).status_code, 302)
        self.assertFalse(QuestionsBuilder.objects.filter(pk=question.pk).exists())

    def test_preview_student_form_results_and_csv_preserve_block_order(self):
        a, b = self.block(), self.block('Блок B')
        a1, b1 = self.question('A1', a, required=True), self.question('B1', b)
        preview = self.client.get(reverse('surveys_builder:preview', args=[self.survey.pk]))
        self.assertContains(preview, a.description)
        self.assertContains(preview, a1.title)
        self.assertContains(preview, b.title)
        self.assertContains(preview, b1.title)
        self.assertNotContains(preview, f'name="q_{a1.pk}"')
        self.assertNotContains(preview, f'name="q_{b1.pk}"')
        assignment = self.publish()
        self.client.force_login(self.student)
        detail_url = reverse('student_surveys:answer', args=[assignment.pk])
        second_page_url = f'{detail_url}?page=2'
        first_page = self.client.get(detail_url)
        self.assertContains(first_page, a.title)
        self.assertNotContains(first_page, b.title)
        self.assertNotContains(first_page, f'name="q_{b1.pk}"')
        invalid = self.client.post(detail_url, {'action': 'next', 'response_revision': 0})
        self.assertContains(invalid, a.title)
        self.assertTrue(invalid.context['form'].errors)
        first_result = self.client.post(detail_url, {
            'action': 'next', 'response_revision': 0, f'q_{a1.pk}': 'Первый ответ',
        })
        self.assertRedirects(first_result, second_page_url)
        second_page = self.client.get(second_page_url)
        self.assertContains(second_page, f'name="q_{b1.pk}"')
        self.assertNotContains(second_page, a.title)
        self.assertNotContains(second_page, f'name="q_{a1.pk}"')
        response_revision = assignment.response.revision
        final_result = self.client.post(second_page_url, {
            'action': 'submit', 'response_revision': response_revision, f'q_{b1.pk}': 'Второй ответ',
        })
        self.assertRedirects(final_result, second_page_url)
        self.assertContains(self.client.get(detail_url), a.title)
        self.assertContains(self.client.get(detail_url), 'Первый ответ')
        self.assertContains(self.client.get(second_page_url), 'Второй ответ')
        student_api = self.client.get(reverse('student_surveys:student_surveys_api:detail', args=[assignment.pk])).json()
        self.assertEqual([item['id'] for item in student_api['survey']['blocks']], [a.pk, b.pk])
        self.client.force_login(self.staff)
        response = SurveyResponse.objects.get(assignment=assignment)
        detail = self.client.get(reverse('surveys_builder:response_detail', args=[self.survey.pk, response.pk]))
        self.assertContains(detail, a.title)
        exported = self.client.get(reverse('surveys_builder:results_csv', args=[self.survey.pk]))
        rows = list(csv.reader(io.StringIO(b''.join(exported.streaming_content).decode('utf-8-sig')), delimiter=';'))
        self.assertEqual(rows[0][-2:], [f'[{a.title}] A1', f'[{b.title}] B1'])
        self.assertEqual(rows[1][-2:], ['Первый ответ', 'Второй ответ'])

    def test_each_block_is_a_page_and_page_answers_do_not_clear_other_blocks(self):
        first = self.block('Первый блок')
        second = self.block('Второй блок')
        first_question = self.question('Вопрос первого блока', first, required=True)
        second_question = self.question('Вопрос второго блока', second, required=True)
        assignment = self.publish()
        self.client.force_login(self.student)
        first_url = reverse('student_surveys:answer', args=[assignment.pk])
        second_url = f'{first_url}?page=2'

        first_page = self.client.get(first_url)
        self.assertContains(first_page, 'Страница 1 из 2')
        self.assertContains(first_page, first_question.title)
        self.assertNotContains(first_page, second_question.title)
        self.assertEqual(self.client.get(f'{first_url}?page=3').status_code, 404)
        self.assertEqual(self.client.post(first_url, {
            'action': 'submit', 'response_revision': 0, f'q_{first_question.pk}': 'Первый',
        }).status_code, 400)

        next_result = self.client.post(first_url, {
            'action': 'next', 'response_revision': 0, f'q_{first_question.pk}': 'Первый',
        })
        self.assertRedirects(next_result, second_url)
        response = SurveyResponse.objects.get(assignment=assignment)
        self.assertEqual(response.answers.get(question=first_question).value, 'Первый')
        second_page = self.client.get(second_url)
        self.assertContains(second_page, 'Страница 2 из 2')
        self.assertContains(second_page, second_question.title)
        self.assertNotContains(second_page, first_question.title)

        saved = self.client.post(second_url, {
            'action': 'save', 'response_revision': response.revision,
            f'q_{second_question.pk}': 'Черновик второго блока',
        })
        self.assertRedirects(saved, second_url)
        response.refresh_from_db()
        self.assertEqual(response.answers.get(question=first_question).value, 'Первый')
        self.assertEqual(response.answers.get(question=second_question).value, 'Черновик второго блока')
        self.assertContains(self.client.get(first_url), 'Первый')

        final = self.client.post(second_url, {
            'action': 'submit', 'response_revision': response.revision,
            f'q_{second_question.pk}': 'Второй',
        })
        self.assertRedirects(final, second_url)
        response.refresh_from_db()
        self.assertEqual(response.status, SurveyResponse.Status.SUBMITTED)
        self.assertEqual(response.answers.get(question=first_question).value, 'Первый')
        self.assertEqual(response.answers.get(question=second_question).value, 'Второй')
        self.assertNotContains(self.client.get(first_url), second_question.title)
        self.assertNotContains(self.client.get(second_url), first_question.title)

    def test_final_page_requires_earlier_answers_and_rejects_other_page_fields(self):
        first = self.block('Первый блок')
        second = self.block('Второй блок')
        first_question = self.question('Обязательный первый', first, required=True)
        second_question = self.question('Обязательный второй', second, required=True)
        assignment = self.publish()
        self.client.force_login(self.student)
        first_url = reverse('student_surveys:answer', args=[assignment.pk])
        second_url = f'{first_url}?page=2'

        tampered = self.client.post(second_url, {
            'action': 'save', 'response_revision': 0,
            f'q_{first_question.pk}': 'Подмена', f'q_{second_question.pk}': 'Второй',
        })
        self.assertContains(tampered, 'Ответ содержит вопрос из другого опросника')
        self.assertFalse(SurveyResponse.objects.filter(assignment=assignment).exists())

        incomplete = self.client.post(second_url, {
            'action': 'submit', 'response_revision': 0, f'q_{second_question.pk}': 'Второй',
        })
        self.assertContains(incomplete, 'Заполните обязательные вопросы на предыдущих страницах', status_code=400)
        self.assertContains(incomplete, 'value="Второй"', status_code=400)
        self.assertFalse(SurveyResponse.objects.filter(assignment=assignment).exists())

    def test_default_block_and_explicit_block_have_their_own_pages(self):
        general_question = self.question('Общий вопрос')
        default_block = general_question.block
        block = self.block('Блок')
        block_question = self.question('Вопрос блока', block)
        assignment = self.publish()
        self.client.force_login(self.student)
        first_url = reverse('student_surveys:answer', args=[assignment.pk])
        self.assertContains(self.client.get(first_url), default_block.title)
        self.assertContains(self.client.get(first_url), general_question.title)
        self.assertNotContains(self.client.get(first_url), block_question.title)
        self.assertContains(self.client.get(f'{first_url}?page=2'), block_question.title)

    def test_block_delete_rolls_back_if_order_update_fails(self):
        block = self.block()
        question = self.question('Q', block)
        with patch.object(QuestionsBuilder.objects, 'bulk_update', side_effect=RuntimeError('test')):
            with self.assertRaises(RuntimeError):
                services.block_action(self.survey.pk, block.pk, self.staff, 'delete', self.version())
        question.refresh_from_db()
        self.assertEqual(question.block_id, block.pk)
        self.assertTrue(self.survey.blocks.filter(pk=block.pk).exists())


class BlockMigrationTests(TransactionTestCase):
    def test_existing_published_answers_and_order_survive_migration(self):
        before = ('surveys_builder', '0004_alter_surveysbuilder_options_and_more')
        after = ('surveys_builder', '0007_merge_block_content_and_response_protection')
        executor = MigrationExecutor(connection)
        executor.migrate([before])
        try:
            apps = executor.loader.project_state([before]).apps
            User = apps.get_model(settings.AUTH_USER_MODEL)
            user = User.objects.create(username='legacy-blocks-user')
            survey = apps.get_model('surveys_builder', 'SurveysBuilder').objects.create(title='Опубликованный', status='published')
            question = apps.get_model('surveys_builder', 'QuestionsBuilder').objects.create(survey=survey, title='Старый вопрос', position=7)
            assignment = apps.get_model('surveys_builder', 'SurveyAssignment').objects.create(survey=survey, user=user)
            response = apps.get_model('surveys_builder', 'SurveyResponse').objects.create(assignment=assignment, status='submitted')
            answer = apps.get_model('surveys_builder', 'SurveyAnswer').objects.create(response=response, question=question, value='Старый ответ')
            MigrationExecutor(connection).migrate([after])
            migrated = QuestionsBuilder.objects.get(pk=question.pk)
            self.assertEqual(migrated.block.title, 'Основной блок')
            self.assertEqual(migrated.position, 7)
            self.assertEqual(SurveysBuilder.objects.get(pk=survey.pk).status, 'published')
            self.assertEqual(SurveyAnswer.objects.get(pk=answer.pk).value, 'Старый ответ')
        finally:
            MigrationExecutor(connection).migrate([after])
