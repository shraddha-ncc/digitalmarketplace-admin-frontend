import mock

from datetime import datetime

from ..helpers import LoggedInApplicationTest


class TestActivity(LoggedInApplicationTest):
    @mock.patch('app.main.activity.data_api_client')
    def test_should_render_activity_page_with_default_date(self, data_api_client):
        today = datetime.now().strftime("%d/%m/%Y")

        response = self.client.get('/admin/audits')
        self.assertEquals(200, response.status_code)

        date_header = """
        <p class="context">
            Activity for
        </p>
        <h1>
        {}
        </h1>
        """.format(today)

        self.assertIn(
            self._replace_whitespace(date_header),
            self._replace_whitespace(response.get_data(as_text=True))
        )

        data_api_client.find_audit_events.assert_called()


    @mock.patch('app.main.activity.data_api_client')
    def test_should_render_correct_form_defaults(self, data_api_client):
        response = self.client.get('/admin/audits')
        self.assertEquals(200, response.status_code)

        self.assertIn(
            '<input class="filter-field-text" id="audit_date" name="audit_date" type="text" value="">',  # noqa
            response.get_data(as_text=True)
        )

        self.assertIn(
            self._replace_whitespace(
                '<input name="acknowledged" value="all" id="acknowledged-1" type="radio" aria-controls="" checked>'),
            # noqa
            self._replace_whitespace(response.get_data(as_text=True))
        )

        data_api_client.find_audit_events.assert_called()


    @mock.patch('app.main.activity.data_api_client')
    def test_should_not_allow_invalid_dates(self, data_api_client):
        response = self.client.get('/admin/audits?audit_date=invalid')
        self.assertEquals(400, response.status_code)
        self.assertIn(
            "Not a valid date value",
            response.get_data(as_text=True)
        )
        self.assertIn(
            '<input class="filter-field-text" id="audit_date" name="audit_date" type="text" value="invalid">',  # noqa
            response.get_data(as_text=True)
        )

        self.assertIn(
            '<div class="validation-masthead" aria-labelledby="validation-masthead-heading">',  # noqa
            response.get_data(as_text=True)
        )

        self.assertIn(
            self._replace_whitespace(
                '<a href="#example-textbox" class="validation-masthead-link"><label for="audit_date">Audit Date</label></a>'),
            # noqa
            self._replace_whitespace(response.get_data(as_text=True))
        )

        data_api_client.find_audit_events.assert_not_called()


    @mock.patch('app.main.activity.data_api_client')
    def test_should_not_allow_invalid_acknowledges(self, data_api_client):
        response = self.client.get('/admin/audits?acknowledged=invalid')
        self.assertEquals(400, response.status_code)

        self.assertIn(
            self._replace_whitespace(
                '<a href="#example-textbox" class="validation-masthead-link"><label for="acknowledged">acknowledged</label></a>'),
            # noqa
            self._replace_whitespace(response.get_data(as_text=True))
        )
        data_api_client.find_audit_events.assert_not_called()

    @mock.patch('app.main.activity.data_api_client')
    def test_should_allow_valid_submission_with_all_fields(self, data_api_client):
        data_api_client.find_audit_events.return_value = {'auditEvents': []}

        response = self.client.get('/admin/audits?audit_date=2006-01-01&acknowledged=all')  # noqa
        self.assertEquals(200, response.status_code)
        self.assertIn(
            '<input class="filter-field-text" id="audit_date" name="audit_date" type="text" value="2006-01-01">',
            # noqa
            response.get_data(as_text=True)
        )

        self.assertIn(
            self._replace_whitespace(
                '<inputname="acknowledged"value="all"id="acknowledged-1"type="radio"aria-controls=""checked>'),  # noqa
            self._replace_whitespace(response.get_data(as_text=True))
        )
        data_api_client.find_audit_events.assert_called()

    @mock.patch('app.main.activity.data_api_client')
    def test_should_allow_valid_submission_with_only_date_fields(self, data_api_client):
        data_api_client.find_audit_events.return_value = {'auditEvents': []}

        response = self.client.get('/admin/audits?audit_date=2006-01-01')  # noqa
        self.assertEquals(200, response.status_code)
        self.assertIn(
            '<input class="filter-field-text" id="audit_date" name="audit_date" type="text" value="2006-01-01">',
            # noqa
            response.get_data(as_text=True)
        )

        self.assertIn(
            self._replace_whitespace(
                '<inputname="acknowledged"value="all"id="acknowledged-1"type="radio"aria-controls=""checked>'),  # noqa
            self._replace_whitespace(response.get_data(as_text=True))
        )
        data_api_client.find_audit_events.assert_called_with(
            audit_date='2006-01-01',
            audit_type='update_service',
            acknowledged='all')

    @mock.patch('app.main.activity.data_api_client')
    def test_should_allow_valid_submission_with_only_acknowledged_fields(self, data_api_client):
        data_api_client.find_audit_events.return_value = {'auditEvents': []}

        response = self.client.get('/admin/audits?acknowledged=not-acknowledged')  # noqa
        self.assertEquals(200, response.status_code)
        self.assertIn(
            '<input class="filter-field-text" id="audit_date" name="audit_date" type="text" value="">',
            # noqa
            response.get_data(as_text=True)
        )

        self.assertIn(
            self._replace_whitespace(
                '<inputname="acknowledged"value="not-acknowledged"id="acknowledged-3"type="radio"aria-controls=""checked>'),  # noqa
            self._replace_whitespace(response.get_data(as_text=True))
        )
        data_api_client.find_audit_events.assert_called_with(
            audit_date=None,
            audit_type='update_service',
            acknowledged='not-acknowledged')

    @mock.patch('app.main.activity.data_api_client')
    def test_should_call_api_with_correct_params(self, data_api_client):
        data_api_client.find_audit_events.return_value = {'auditEvents': []}

        response = self.client.get('/admin/audits?audit_date=2006-01-01&acknowledged=all')  # noqa
        self.assertEquals(200, response.status_code)

        data_api_client.find_audit_events.assert_called_with(
            audit_type='update_service',
            audit_date='2006-01-01',
            acknowledged='all')

    @mock.patch('app.main.activity.data_api_client')
    def test_should_call_api_with_none_date(self, data_api_client):
        data_api_client.find_audit_events.return_value = {'auditEvents': []}

        response = self.client.get('/admin/audits?acknowledged=all')  # noqa
        self.assertEquals(200, response.status_code)

        data_api_client.find_audit_events.assert_called_with(
            audit_type='update_service',
            audit_date=None,
            acknowledged='all')

    @mock.patch('app.main.activity.data_api_client')
    def test_should_render_activity_page_with_submitted_date(self, data_api_client):
        response = self.client.get('/admin/audits?audit_date=2010-01-01')

        self.assertEquals(200, response.status_code)

        date_header = """
        <p class="context">
            Activity for
        </p>
        <h1>
        01/01/2010
        </h1>
        """

        self.assertIn(
            self._replace_whitespace(date_header),
            self._replace_whitespace(response.get_data(as_text=True))
        )

        data_api_client.find_audit_events.assert_called()



