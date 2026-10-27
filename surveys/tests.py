from unittest.mock import patch

from django import forms
from django.contrib.auth import get_user_model
from django.core.exceptions import PermissionDenied
from django.db import IntegrityError, transaction
from django.test import TestCase
from django.urls import reverse

from scholar_form.models import UserInfo
from surveys_builder.forms import QuestionsBuilderForm
from surveys_builder.models import QuestionOptions, QuestionsBuilder, SurveysBuilder

from .forms import SurveyResponseForm
from .models import SurveyAnswer, SurveySubmission
from .services import submit_survey_response


def bound_survey_form(survey, answers, displayed_form=None):
    displayed_form = displayed_form or SurveyResponseForm(survey=survey)
    data = {
        "submission_key": str(displayed_form["submission_key"].value()),
        "survey_fingerprint": displayed_form.fingerprint,
        **answers,
    }
    return SurveyResponseForm(data, survey=survey)


class SurveyStorageTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.user = get_user_model().objects.create_user(username="survey-student")
        cls.survey = SurveysBuilder.objects.create(
            title="Первый опрос", description="Исходное описание"
        )
        cls.question = QuestionsBuilder.objects.create(
            survey=cls.survey,
            title="Исходный вопрос?",
            answer_type=QuestionsBuilder.AnswerType.SINGLE_CHOICE,
            required=True,
            position=1,
        )
        cls.option = QuestionOptions.objects.create(
            question=cls.question, text="Исходный вариант", position=1
        )

    def test_snapshots_survive_builder_edits_and_deletion(self):
        submission = SurveySubmission.from_survey(user=self.user, survey=self.survey)
        submission.save()
        answer = SurveyAnswer.from_question(
            submission=submission, question=self.question, option=self.option
        )
        answer.save()

        self.survey.title = "Новое название"
        self.survey.save(update_fields=["title"])
        form = QuestionsBuilderForm(
            {
                "title": "Новый вопрос?",
                "answer_type": QuestionsBuilder.AnswerType.SINGLE_CHOICE,
                "required": "on",
                "options_text": "Новый вариант\nЕщё вариант",
            },
            instance=self.question,
        )
        self.assertTrue(form.is_valid(), form.errors)
        form.save()
        form.save_options(self.question)

        answer.refresh_from_db()
        self.assertIsNone(answer.option_id)
        self.assertEqual(answer.option_text, "Исходный вариант")
        self.assertEqual(answer.question_title, "Исходный вопрос?")

        survey_id = self.survey.pk
        question_id = self.question.pk
        option_id = self.option.pk
        self.survey.delete()
        submission.refresh_from_db()
        answer.refresh_from_db()
        self.assertIsNone(submission.survey_id)
        self.assertEqual(submission.source_survey_id, survey_id)
        self.assertEqual(submission.survey_title, "Первый опрос")
        self.assertEqual(submission.survey_description, "Исходное описание")
        self.assertIsNone(answer.question_id)
        self.assertEqual(answer.source_question_id, question_id)
        self.assertEqual(answer.source_option_id, option_id)
        self.assertEqual(answer.option_text, "Исходный вариант")

    def test_one_answer_per_question_per_submission(self):
        submission = SurveySubmission.from_survey(user=self.user, survey=self.survey)
        submission.save()
        answer = SurveyAnswer.from_question(
            submission=submission, question=self.question, option=self.option
        )
        answer.save()

        with self.assertRaises(IntegrityError), transaction.atomic():
            SurveyAnswer.from_question(
                submission=submission, question=self.question, option=self.option
            ).save()

        self.assertEqual(submission.answers.count(), 1)

    def test_option_from_another_question_is_rejected(self):
        other_question = QuestionsBuilder.objects.create(
            survey=self.survey, title="Другой вопрос"
        )
        other_option = QuestionOptions.objects.create(
            question=other_question, text="Чужой вариант"
        )
        submission = SurveySubmission.from_survey(user=self.user, survey=self.survey)

        with self.assertRaises(ValueError):
            SurveyAnswer.from_question(
                submission=submission,
                question=self.question,
                option=other_option,
            )


class SurveyResponseFormTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.survey = SurveysBuilder.objects.create(title="Опрос")
        cls.long_question = QuestionsBuilder.objects.create(
            survey=cls.survey,
            title="Расскажите подробнее",
            answer_type=QuestionsBuilder.AnswerType.LONG_TEXT,
            position=2,
        )
        cls.short_question = QuestionsBuilder.objects.create(
            survey=cls.survey,
            title="Как вас зовут?",
            answer_type=QuestionsBuilder.AnswerType.SHORT_TEXT,
            required=True,
            position=1,
        )
        cls.choice_question = QuestionsBuilder.objects.create(
            survey=cls.survey,
            title="Выберите вариант",
            answer_type=QuestionsBuilder.AnswerType.SINGLE_CHOICE,
            required=True,
            position=3,
        )
        cls.second_option = QuestionOptions.objects.create(
            question=cls.choice_question, text="Второй", position=2
        )
        cls.first_option = QuestionOptions.objects.create(
            question=cls.choice_question, text="Первый", position=1
        )

    def test_question_order_and_field_types(self):
        form = SurveyResponseForm(survey=self.survey)

        self.assertEqual(
            [field_name for _, field_name in form.question_fields],
            [
                f"question_{self.short_question.pk}",
                f"question_{self.long_question.pk}",
                f"question_{self.choice_question.pk}",
            ],
        )
        self.assertIsInstance(
            form.fields[f"question_{self.short_question.pk}"].widget,
            forms.TextInput,
        )
        self.assertIsInstance(
            form.fields[f"question_{self.long_question.pk}"].widget,
            forms.Textarea,
        )
        choice_field = form.fields[f"question_{self.choice_question.pk}"]
        self.assertIsInstance(choice_field.widget, forms.RadioSelect)
        self.assertEqual(
            list(choice_field.queryset),
            [self.first_option, self.second_option],
        )

    def test_valid_answers_and_optional_blank(self):
        form = bound_survey_form(
            self.survey,
            {
                f"question_{self.short_question.pk}": "  Анна  ",
                f"question_{self.choice_question.pk}": str(self.first_option.pk),
            },
        )

        self.assertTrue(form.is_valid(), form.errors)
        self.assertEqual(
            form.cleaned_data[f"question_{self.short_question.pk}"], "Анна"
        )
        self.assertEqual(
            form.cleaned_data[f"question_{self.long_question.pk}"], ""
        )
        self.assertEqual(
            form.cleaned_data[f"question_{self.choice_question.pk}"],
            self.first_option,
        )

    def test_multiline_answer_and_optional_choice(self):
        optional_choice = QuestionsBuilder.objects.create(
            survey=self.survey,
            title="Необязательный выбор",
            answer_type=QuestionsBuilder.AnswerType.SINGLE_CHOICE,
            position=4,
        )
        QuestionOptions.objects.create(question=optional_choice, text="Вариант")
        form = bound_survey_form(
            self.survey,
            {
                f"question_{self.short_question.pk}": "Анна",
                f"question_{self.long_question.pk}": "Первая строка\nВторая строка",
                f"question_{self.choice_question.pk}": str(self.first_option.pk),
            },
        )

        self.assertTrue(form.is_valid(), form.errors)
        self.assertEqual(
            form.cleaned_data[f"question_{self.long_question.pk}"],
            "Первая строка\nВторая строка",
        )
        self.assertIsNone(form.cleaned_data[f"question_{optional_choice.pk}"])

    def test_required_questions_reject_blank_values(self):
        form = bound_survey_form(
            self.survey,
            {f"question_{self.short_question.pk}": "   "},
        )

        self.assertFalse(form.is_valid())
        self.assertIn(f"question_{self.short_question.pk}", form.errors)
        self.assertIn(f"question_{self.choice_question.pk}", form.errors)
        self.assertNotIn(f"question_{self.long_question.pk}", form.errors)

    def test_choice_from_another_question_is_rejected(self):
        other_question = QuestionsBuilder.objects.create(
            survey=self.survey,
            title="Другой выбор",
            answer_type=QuestionsBuilder.AnswerType.SINGLE_CHOICE,
        )
        foreign_option = QuestionOptions.objects.create(
            question=other_question, text="Чужой вариант"
        )
        form = bound_survey_form(
            self.survey,
            {
                f"question_{self.short_question.pk}": "Анна",
                f"question_{self.choice_question.pk}": str(foreign_option.pk),
            },
        )

        self.assertFalse(form.is_valid())
        self.assertIn(f"question_{self.choice_question.pk}", form.errors)

    def test_empty_survey_is_invalid(self):
        empty_survey = SurveysBuilder.objects.create(title="Пустой опрос")
        form = bound_survey_form(empty_survey, {})

        self.assertFalse(form.is_valid())
        self.assertIn("В опросе пока нет вопросов.", form.non_field_errors())


class SurveySubmissionServiceTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.user = get_user_model().objects.create_user(username="response-student")
        cls.survey = SurveysBuilder.objects.create(
            title="Учебный опрос", description="Описание"
        )
        cls.short_question = QuestionsBuilder.objects.create(
            survey=cls.survey,
            title="Короткий ответ",
            answer_type=QuestionsBuilder.AnswerType.SHORT_TEXT,
            required=True,
            position=1,
        )
        cls.long_question = QuestionsBuilder.objects.create(
            survey=cls.survey,
            title="Развёрнутый ответ",
            answer_type=QuestionsBuilder.AnswerType.LONG_TEXT,
            position=2,
        )
        cls.choice_question = QuestionsBuilder.objects.create(
            survey=cls.survey,
            title="Выбор",
            answer_type=QuestionsBuilder.AnswerType.SINGLE_CHOICE,
            required=True,
            position=3,
        )
        cls.option = QuestionOptions.objects.create(
            question=cls.choice_question, text="Да", position=1
        )
        QuestionOptions.objects.create(
            question=cls.choice_question, text="Нет", position=2
        )

    def response_form(self, *, displayed_form=None, long_answer="Подробный ответ"):
        return bound_survey_form(
            self.survey,
            {
                f"question_{self.short_question.pk}": "Кратко",
                f"question_{self.long_question.pk}": long_answer,
                f"question_{self.choice_question.pk}": str(self.option.pk),
            },
            displayed_form=displayed_form,
        )

    def test_saves_all_answer_types_and_skips_blank_optional_answer(self):
        form = self.response_form(long_answer="")

        submission, created = submit_survey_response(user=self.user, form=form)

        self.assertTrue(created)
        self.assertEqual(submission.user, self.user)
        self.assertEqual(submission.survey_title, "Учебный опрос")
        self.assertEqual(submission.survey_description, "Описание")
        self.assertEqual(submission.submission_key, form.cleaned_data["submission_key"])
        self.assertEqual(
            list(submission.answers.values_list("source_question_id", flat=True)),
            [self.short_question.pk, self.choice_question.pk],
        )
        short_answer, choice_answer = list(submission.answers.all())
        self.assertEqual(short_answer.text_value, "Кратко")
        self.assertEqual(short_answer.question_type, "short_text")
        self.assertEqual(choice_answer.source_option_id, self.option.pk)
        self.assertEqual(choice_answer.option_text, "Да")

    def test_saves_multiline_answer(self):
        form = self.response_form(long_answer="Первая строка\nВторая строка")

        submission, created = submit_survey_response(user=self.user, form=form)

        self.assertTrue(created)
        answer = submission.answers.get(source_question_id=self.long_question.pk)
        self.assertEqual(answer.text_value, "Первая строка\nВторая строка")
        self.assertEqual(answer.question_type, "long_text")

    def test_repeated_form_returns_existing_submission_after_builder_edit(self):
        form = self.response_form()
        first_submission, created = submit_survey_response(user=self.user, form=form)
        self.assertTrue(created)

        self.choice_question.title = "Новая формулировка"
        self.choice_question.save(update_fields=["title"])
        repeated_form = SurveyResponseForm(form.data, survey=self.survey)
        second_submission, created = submit_survey_response(
            user=self.user, form=repeated_form
        )

        self.assertFalse(created)
        self.assertEqual(second_submission.pk, first_submission.pk)
        self.assertEqual(SurveySubmission.objects.count(), 1)
        self.assertEqual(SurveyAnswer.objects.count(), 3)

    def test_changed_survey_is_not_submitted(self):
        displayed_form = SurveyResponseForm(survey=self.survey)
        self.option.text = "Изменённый вариант"
        self.option.save(update_fields=["text"])
        form = self.response_form(displayed_form=displayed_form)

        submission, created = submit_survey_response(user=self.user, form=form)

        self.assertIsNone(submission)
        self.assertFalse(created)
        self.assertTrue(form.non_field_errors())
        self.assertFalse(SurveySubmission.objects.exists())

    def test_tampered_fingerprint_is_rejected(self):
        form = self.response_form()
        form.data["survey_fingerprint"] = "invalid"

        submission, created = submit_survey_response(user=self.user, form=form)

        self.assertIsNone(submission)
        self.assertFalse(created)
        self.assertTrue(form.non_field_errors())
        self.assertFalse(SurveySubmission.objects.exists())

    def test_submission_rolls_back_if_answer_write_fails(self):
        form = self.response_form()
        original_save = SurveyAnswer.save
        save_calls = 0

        def fail_on_second_answer(answer, *args, **kwargs):
            nonlocal save_calls
            save_calls += 1
            if save_calls == 2:
                raise RuntimeError("Answer write failed")
            return original_save(answer, *args, **kwargs)

        with patch.object(SurveyAnswer, "save", fail_on_second_answer):
            with self.assertRaises(RuntimeError):
                submit_survey_response(user=self.user, form=form)

        self.assertFalse(SurveySubmission.objects.exists())
        self.assertFalse(SurveyAnswer.objects.exists())

    def test_same_key_cannot_be_reused_by_another_user(self):
        form = self.response_form()
        submit_survey_response(user=self.user, form=form)
        other_user = get_user_model().objects.create_user(username="other-student")

        with self.assertRaises(PermissionDenied):
            submit_survey_response(
                user=other_user,
                form=SurveyResponseForm(form.data, survey=self.survey),
            )

        self.assertEqual(SurveySubmission.objects.count(), 1)

    def test_different_keys_allow_separate_submissions(self):
        first_submission, _ = submit_survey_response(
            user=self.user, form=self.response_form()
        )
        second_submission, _ = submit_survey_response(
            user=self.user, form=self.response_form()
        )

        self.assertNotEqual(first_submission.pk, second_submission.pk)
        self.assertEqual(SurveySubmission.objects.count(), 2)


class SurveyStudentPageTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.user = get_user_model().objects.create_user(username="survey-page-student")
        UserInfo.objects.create(user=cls.user, status="SCHOLAR")
        cls.survey = SurveysBuilder.objects.create(title="Опрос ученика")
        cls.text_question = QuestionsBuilder.objects.create(
            survey=cls.survey,
            title="Ваш ответ",
            answer_type=QuestionsBuilder.AnswerType.SHORT_TEXT,
            required=True,
            position=1,
        )
        cls.choice_question = QuestionsBuilder.objects.create(
            survey=cls.survey,
            title="Выберите ответ",
            answer_type=QuestionsBuilder.AnswerType.SINGLE_CHOICE,
            required=True,
            position=2,
        )
        cls.option = QuestionOptions.objects.create(
            question=cls.choice_question, text="Подходит", position=1
        )
        QuestionOptions.objects.create(
            question=cls.choice_question, text="Не подходит", position=2
        )

    def setUp(self):
        self.url = reverse("surveys:take", args=[self.survey.pk])

    def form_data(self, form):
        return {
            "submission_key": str(form["submission_key"].value()),
            "survey_fingerprint": form.fingerprint,
            f"question_{self.text_question.pk}": "Ответ ученика",
            f"question_{self.choice_question.pk}": str(self.option.pk),
        }

    def test_anonymous_user_is_redirected_to_login(self):
        response = self.client.get(self.url)

        self.assertEqual(response.status_code, 302)
        self.assertIn(reverse("login"), response["Location"])

    def test_unassigned_user_cannot_open_or_submit_survey(self):
        self.client.force_login(self.user)

        self.assertEqual(self.client.get(self.url).status_code, 403)
        self.assertEqual(self.client.post(self.url, {}).status_code, 403)
        self.assertFalse(SurveySubmission.objects.exists())

    def test_user_without_profile_cannot_open_survey(self):
        user_without_profile = get_user_model().objects.create_user(
            username="survey-user-without-profile"
        )
        self.client.force_login(user_without_profile)

        self.assertEqual(self.client.get(self.url).status_code, 403)

    def test_assigned_user_can_open_submit_and_view_confirmation(self):
        self.client.force_login(self.user)
        with patch("surveys.views.is_survey_assigned", return_value=True):
            page = self.client.get(self.url)
            self.assertEqual(page.status_code, 200)
            self.assertContains(page, "Опрос ученика")
            self.assertContains(page, "Ваш ответ")
            self.assertContains(page, "Выберите ответ")
            response = self.client.post(self.url, self.form_data(page.context["form"]))

        submission = SurveySubmission.objects.get(user=self.user)
        self.assertRedirects(
            response, reverse("surveys:complete", args=[submission.pk])
        )
        self.assertEqual(submission.answers.count(), 2)

    def test_assignment_is_checked_again_on_post(self):
        self.client.force_login(self.user)
        with patch("surveys.views.is_survey_assigned", return_value=True):
            page = self.client.get(self.url)

        response = self.client.post(self.url, self.form_data(page.context["form"]))

        self.assertEqual(response.status_code, 403)
        self.assertFalse(SurveySubmission.objects.exists())

    def test_invalid_post_shows_errors_without_saving(self):
        self.client.force_login(self.user)
        with patch("surveys.views.is_survey_assigned", return_value=True):
            page = self.client.get(self.url)
            data = self.form_data(page.context["form"])
            data.pop(f"question_{self.text_question.pk}")
            response = self.client.post(self.url, data)

        self.assertEqual(response.status_code, 200)
        self.assertIn(f"question_{self.text_question.pk}", response.context["form"].errors)
        self.assertFalse(SurveySubmission.objects.exists())

    def test_post_rejects_option_from_another_survey(self):
        other_survey = SurveysBuilder.objects.create(title="Другой опрос")
        other_question = QuestionsBuilder.objects.create(
            survey=other_survey,
            title="Чужой вопрос",
            answer_type=QuestionsBuilder.AnswerType.SINGLE_CHOICE,
        )
        foreign_option = QuestionOptions.objects.create(
            question=other_question, text="Чужой вариант"
        )
        self.client.force_login(self.user)
        with patch("surveys.views.is_survey_assigned", return_value=True):
            page = self.client.get(self.url)
            data = self.form_data(page.context["form"])
            data[f"question_{self.choice_question.pk}"] = str(foreign_option.pk)
            response = self.client.post(self.url, data)

        self.assertEqual(response.status_code, 200)
        self.assertIn(f"question_{self.choice_question.pk}", response.context["form"].errors)
        self.assertFalse(SurveySubmission.objects.exists())

    def test_post_ignores_forged_question_id(self):
        other_survey = SurveysBuilder.objects.create(title="Другой опрос")
        other_question = QuestionsBuilder.objects.create(
            survey=other_survey, title="Чужой вопрос"
        )
        self.client.force_login(self.user)
        with patch("surveys.views.is_survey_assigned", return_value=True):
            page = self.client.get(self.url)
            data = self.form_data(page.context["form"])
            data[f"question_{other_question.pk}"] = "Подставленный ответ"
            response = self.client.post(self.url, data)

        self.assertEqual(response.status_code, 302)
        submission = SurveySubmission.objects.get(user=self.user)
        self.assertEqual(
            set(submission.answers.values_list("source_question_id", flat=True)),
            {self.text_question.pk, self.choice_question.pk},
        )

    def test_invalid_submission_key_is_rejected(self):
        self.client.force_login(self.user)
        with patch("surveys.views.is_survey_assigned", return_value=True):
            page = self.client.get(self.url)
            data = self.form_data(page.context["form"])
            data["submission_key"] = "invalid"
            response = self.client.post(self.url, data)

        self.assertEqual(response.status_code, 200)
        self.assertIn("submission_key", response.context["form"].errors)
        self.assertFalse(SurveySubmission.objects.exists())

    def test_repeated_post_redirects_to_same_confirmation(self):
        self.client.force_login(self.user)
        with patch("surveys.views.is_survey_assigned", return_value=True):
            page = self.client.get(self.url)
            data = self.form_data(page.context["form"])
            first = self.client.post(self.url, data)
            second = self.client.post(self.url, data)

        self.assertEqual(first["Location"], second["Location"])
        self.assertEqual(SurveySubmission.objects.count(), 1)
        self.assertEqual(SurveyAnswer.objects.count(), 2)

    def test_changed_survey_post_shows_error_without_saving(self):
        self.client.force_login(self.user)
        with patch("surveys.views.is_survey_assigned", return_value=True):
            page = self.client.get(self.url)
            data = self.form_data(page.context["form"])
            self.option.text = "Изменённый вариант"
            self.option.save(update_fields=["text"])
            response = self.client.post(self.url, data)

        self.assertEqual(response.status_code, 200)
        self.assertTrue(response.context["form"].non_field_errors())
        self.assertFalse(SurveySubmission.objects.exists())

    def test_confirmation_is_only_visible_to_submission_owner(self):
        submission = SurveySubmission.from_survey(user=self.user, survey=self.survey)
        submission.save()
        url = reverse("surveys:complete", args=[submission.pk])
        other_user = get_user_model().objects.create_user(username="other-page-student")

        self.client.force_login(other_user)
        self.assertEqual(self.client.get(url).status_code, 404)

        self.client.force_login(self.user)
        response = self.client.get(url)
        self.assertContains(response, "Ответы отправлены")

    def test_confirmation_survives_deletion_of_original_survey(self):
        submission = SurveySubmission.from_survey(user=self.user, survey=self.survey)
        submission.save()
        url = reverse("surveys:complete", args=[submission.pk])
        self.survey.delete()
        self.client.force_login(self.user)

        response = self.client.get(url)

        self.assertContains(response, "Опрос ученика")
        self.assertContains(response, "Ответы отправлены")
