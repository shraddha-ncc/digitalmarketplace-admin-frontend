from collections import OrderedDict
from itertools import groupby, chain
from operator import itemgetter

from dateutil.parser import parse as parse_date
from dmcontent.errors import ContentNotFoundError
from dmcontent.utils import count_unanswered_questions
from dmapiclient import HTTPError, APIError
from dmapiclient.audit import AuditTypes
from dmutils import s3
from dmutils.config import convert_to_boolean
from dmutils.documents import (
    AGREEMENT_FILENAME, COUNTERPART_FILENAME,
    file_is_pdf, get_document_path, get_extension, get_signed_url,
    generate_timestamped_document_upload_path, degenerate_document_path_and_return_doc_name,
    generate_download_filename)
from dmutils.email import send_user_account_email
from dmutils.flask import timed_render_template as render_template
from dmutils.forms.helpers import get_errors_from_wtform
from dmutils.formats import datetimeformat
from dmutils.urls import rewrite_supplier_asset_path
from flask import request, redirect, url_for, abort, current_app, flash
from flask_login import current_user

from .. import main
from ..auth import role_required
from ..forms import (
    EmailAddressForm, MoveUserForm,
    EditSupplierCompanyRegistrationNumberForm,
    EditSupplierRegisteredAddressForm,
    EditSupplierRegisteredNameForm
)
from ..helpers.countries import COUNTRY_TUPLE
from ..helpers.pagination import get_nav_args_from_api_response_links
from ..helpers.supplier_details import (
    get_supplier_frameworks_visible_for_role,
    get_company_details_from_supplier,
    DEPRECATED_FRAMEWORK_SLUGS,
)
from ... import data_api_client, content_loader


AGREEMENT_ON_HOLD_MESSAGE = 'The agreement for {organisation_name} was put on hold.'
AGREEMENT_APPROVED_MESSAGE = 'The agreement for {organisation_name} was approved. ' \
                             'They will receive a countersigned version soon.'
AGREEMENT_APPROVAL_CANCELLED_MESSAGE = 'The agreement for {organisation_name} had its approval cancelled. ' \
                                       'You can approve it again at any time.'
UPLOAD_COUNTERSIGNED_AGREEMENT_MESSAGE = "Countersigned agreement file was uploaded"
COUNTERSIGNED_AGREEMENT_NOT_PDF_MESSAGE = "Countersigned agreement file is not a PDF"
SUPPLIER_SERVICES_REMOVED_MESSAGE = "You suspended all {framework_name} services for ‘{supplier_name}’."
SUPPLIER_SERVICES_UNSUSPENDED_MESSAGE = "You unsuspended all {framework_name} services for ‘{supplier_name}’."
SUPPLIER_SERVICES_DELAYED_INDEX_MESSAGE = "Search results may take a few minutes to be updated."
SUPPLIER_USER_MESSAGES = {
    'user_invited': 'User invited',
    'user_moved': 'User moved to this supplier',
    'user_not_moved': 'User not moved to this supplier - please check you entered the address of an '
                      'existing supplier user'
}
SUPPLIER_DETAILS_UPDATED_MESSAGE = "The details for ‘{supplier_name}’ have been updated."
OLDEST_INTERESTING_FRAMEWORK_SLUG = 'g-cloud-7'
OLD_SIGNING_FLOW_SLUGS = ['g-cloud-7', 'digital-outcomes-and-specialists']


@main.route('/suppliers', methods=['GET'])
@role_required(
    'admin', 'admin-ccs-category', 'admin-ccs-sourcing', 'admin-framework-manager', 'admin-ccs-data-controller'
)
def find_suppliers():
    if request.args.get("supplier_id"):
        suppliers = [data_api_client.get_supplier(request.args.get("supplier_id"))['suppliers']]
        links = {}
    else:
        duns_number = request.args.get("supplier_duns_number").strip() \
            if request.args.get("supplier_duns_number") else None
        suppliers_response = data_api_client.find_suppliers(
            name=request.args.get("supplier_name"),
            duns_number=duns_number,
            company_registration_number=request.args.get("supplier_company_registration_number"),
            page=request.args.get("page", 1)  # API will validate page number values
        )
        suppliers = suppliers_response['suppliers']
        links = suppliers_response["links"]

    frameworks = data_api_client.find_frameworks()['frameworks']
    try:
        oldest_interesting_framework_id = [
            fw for fw in frameworks if fw['slug'] == OLDEST_INTERESTING_FRAMEWORK_SLUG
        ][0]['id']
    except IndexError:
        current_app.logger.error(f'No framework found with slug: "{OLDEST_INTERESTING_FRAMEWORK_SLUG}"')
        abort(500)

    interesting_frameworks = sorted(
        [framework for framework in frameworks if framework['id'] >= oldest_interesting_framework_id
            and framework['status'] != 'coming'],
        key=lambda fw: fw['id'],
        reverse=True,
    )

    return render_template(
        "view_suppliers.html",
        suppliers=suppliers,
        agreement_filename=AGREEMENT_FILENAME,
        interesting_frameworks=interesting_frameworks,
        old_flow_slugs=OLD_SIGNING_FLOW_SLUGS,
        prev_link=get_nav_args_from_api_response_links(links, 'prev', request.args, ['supplier_name']),
        next_link=get_nav_args_from_api_response_links(links, 'next', request.args, ['supplier_name']),
    )


@main.route("/suppliers/<int:supplier_id>", methods=["GET"])
@role_required(
    "admin", "admin-ccs-category", "admin-ccs-data-controller", "admin-framework-manager", "admin-ccs-sourcing"
)
def supplier_details(supplier_id):
    frameworks = data_api_client.find_frameworks()["frameworks"]
    supplier = data_api_client.get_supplier(supplier_id)["suppliers"]
    supplier_frameworks = data_api_client.get_supplier_frameworks(supplier_id)["frameworkInterest"]

    # Get SupplierFrameworks for frameworks the role is interested in, sorted by oldest frameworkLiveAtUTC first
    visible_supplier_frameworks = get_supplier_frameworks_visible_for_role(
        supplier_frameworks, current_user, frameworks
    )

    # Get the company details
    company_details = get_company_details_from_supplier(supplier)

    return render_template(
        "supplier_details.html",
        company_details=company_details,
        supplier=supplier,
        supplier_id=supplier_id,
        supplier_frameworks=visible_supplier_frameworks,
        old_interesting_framework_slugs=OLD_SIGNING_FLOW_SLUGS,
    )


@main.route('/suppliers/<int:supplier_id>/edit/name', methods=['GET'])
@role_required('admin', 'admin-ccs-category', 'admin-ccs-data-controller')
def edit_supplier_name(supplier_id):
    supplier = data_api_client.get_supplier(supplier_id)

    return render_template(
        "edit_supplier_name.html",
        supplier=supplier["suppliers"]
    )


@main.route('/suppliers/<int:supplier_id>/edit/name', methods=['POST'])
@role_required('admin', 'admin-ccs-category', 'admin-ccs-data-controller')
def update_supplier_name(supplier_id):
    supplier = data_api_client.get_supplier(supplier_id)
    new_supplier_name = request.form.get('new_supplier_name', '').strip()

    data_api_client.update_supplier(
        supplier['suppliers']['id'], {'name': new_supplier_name}, current_user.email_address
    )
    flash(SUPPLIER_DETAILS_UPDATED_MESSAGE.format(supplier_name=new_supplier_name))
    return redirect(url_for('.supplier_details', supplier_id=supplier_id))


@main.route('/suppliers/<int:supplier_id>/edit/registered-name', methods=['GET', 'POST'])
@role_required('admin-ccs-data-controller')
def edit_supplier_registered_name(supplier_id):
    supplier = data_api_client.get_supplier(supplier_id)['suppliers']

    company_details = get_company_details_from_supplier(supplier)

    form = EditSupplierRegisteredNameForm(
        data={"registered_company_name": company_details.get('registered_name')}
    )

    if form.validate_on_submit():
        data_api_client.update_supplier(
            supplier_id,
            {'registeredName': form.registered_company_name.data},
            current_user.email_address
        )
        flash(SUPPLIER_DETAILS_UPDATED_MESSAGE.format(supplier_name=supplier['name']))

        return redirect(url_for('.supplier_details', supplier_id=supplier_id))

    errors = get_errors_from_wtform(form)

    return render_template(
        "suppliers/edit_registered_name.html",
        supplier=supplier,
        form=form,
        errors=errors,
    ), 200 if not errors else 400


@main.route('/suppliers/<int:supplier_id>/edit/registered-company-number', methods=['GET', 'POST'])
@role_required('admin-ccs-data-controller')
def edit_supplier_registered_company_number(supplier_id):
    supplier = data_api_client.get_supplier(supplier_id)['suppliers']
    frameworks = data_api_client.find_frameworks()['frameworks']

    # Take the registered company numbers from the supplier, as we need to know which type it is (CH or other)
    prefill_data = {
        "companies_house_number": supplier.get("companiesHouseNumber"),
        "other_company_registration_number": supplier.get('otherCompanyRegistrationNumber')
    }
    form = EditSupplierCompanyRegistrationNumberForm(data=prefill_data)

    if form.validate_on_submit():
        if form.companies_house_number.data:
            update_supplier_payload = {
                "companiesHouseNumber": form.companies_house_number.data.upper(),
                "otherCompanyRegistrationNumber": None
            }
            update_declaration_payload = {
                "supplierCompanyRegistrationNumber": form.companies_house_number.data.upper()
            }
        else:
            update_supplier_payload = {
                "companiesHouseNumber": None,
                "otherCompanyRegistrationNumber": form.other_company_registration_number.data
            }
            update_declaration_payload = {
                "supplierCompanyRegistrationNumber": form.other_company_registration_number.data
            }

        # Although we've fetched the reg number from the supplier, any recent declaration should be updated too
        supplier_frameworks = data_api_client.get_supplier_frameworks(supplier_id)["frameworkInterest"]
        visible_supplier_frameworks = get_supplier_frameworks_visible_for_role(
            supplier_frameworks, current_user, frameworks
        )
        most_recent_supplier_framework = visible_supplier_frameworks[-1] if visible_supplier_frameworks else {}

        # Update supplier
        data_api_client.update_supplier(
            supplier_id=supplier_id,
            supplier=update_supplier_payload,
            user=current_user.email_address
        )

        if most_recent_supplier_framework.get('declaration'):
            data_api_client.update_supplier_declaration(
                supplier_id,
                most_recent_supplier_framework['frameworkSlug'],
                update_declaration_payload,
                current_user.email_address
            )

        flash(SUPPLIER_DETAILS_UPDATED_MESSAGE.format(supplier_name=supplier['name']))
        return redirect(url_for('.supplier_details', supplier_id=supplier_id))

    errors = get_errors_from_wtform(form)

    return render_template(
        "suppliers/edit_registered_company_number.html",
        supplier=supplier,
        form=form,
        errors=errors,
    ), 200 if not errors else 400


@main.route('/suppliers/<int:supplier_id>/edit/registered-address', methods=['GET', 'POST'])
@role_required('admin-ccs-data-controller')
def edit_supplier_registered_address(supplier_id):
    supplier = data_api_client.get_supplier(supplier_id)['suppliers']
    contact = supplier["contactInformation"][0]

    company_details = get_company_details_from_supplier(supplier)

    prefill_data = {
        "street": company_details['address'].get('street_address_line_1'),
        "city": company_details['address'].get('locality'),
        "postcode": company_details['address'].get('postcode'),
        "country": company_details['address']['country']
    }
    form = EditSupplierRegisteredAddressForm(data=prefill_data)

    if form.validate_on_submit():
        # Will update declarations for open frameworks
        data_api_client.update_supplier(
            supplier_id,
            {'registrationCountry': form.country.data},
            current_user.email_address
        )
        # Will update declarations for open frameworks
        data_api_client.update_contact_information(
            supplier_id,
            contact['id'],
            {
                'country': form.country.data,
                'address1': form.street.data,
                'city': form.city.data,
                'postcode': form.postcode.data,
            },
            current_user.email_address
        )

        flash(SUPPLIER_DETAILS_UPDATED_MESSAGE.format(supplier_name=supplier['name']))
        return redirect(url_for('.supplier_details', supplier_id=supplier_id))

    errors = get_errors_from_wtform(form)

    return render_template(
        "suppliers/edit_registered_address.html",
        supplier=supplier,
        form=form,
        countries=COUNTRY_TUPLE,
        errors=errors,
    ), 200 if not errors else 400


@main.route('/suppliers/<int:supplier_id>/edit/duns-number', methods=['GET'])
@role_required('admin-ccs-data-controller')
def edit_supplier_duns_number(supplier_id):
    # Show 'contact support' template
    supplier = data_api_client.get_supplier(supplier_id)
    return render_template(
        "suppliers/edit_duns_number.html",
        supplier=supplier["suppliers"]
    )


@main.route('/suppliers/<int:supplier_id>/edit/declarations/<string:framework_slug>', methods=['GET'])
@role_required('admin-ccs-sourcing')
def view_supplier_declaration(supplier_id, framework_slug):
    if framework_slug in DEPRECATED_FRAMEWORK_SLUGS:
        abort(404)

    supplier = data_api_client.get_supplier(supplier_id)['suppliers']
    framework = data_api_client.get_framework(framework_slug)['frameworks']
    if framework['status'] not in ('pending', 'standstill', 'live', 'expired',):
        abort(403)
    try:
        sf = data_api_client.get_supplier_framework_info(supplier_id, framework_slug)["frameworkInterest"]
    except APIError as e:
        if e.status_code != 404:
            raise
        sf = {}

    content = content_loader.get_manifest(
        framework_slug,
        'declaration',
    ).filter(sf.get("declaration", {}), inplace_allowed=True)
    declaration_sections = content.sections
    question_content = OrderedDict(
        (question.id, question)
        for question in sorted(
            chain.from_iterable(section.questions for section in declaration_sections),
            key=lambda question: question.number
        )
    )
    modern_slavery_fields = ['modernSlaveryStatement', 'modernSlaveryStatementOptional']
    declaration_with_public_assets = sf.get("declaration", {})
    for field in modern_slavery_fields:
        if declaration_with_public_assets.get(field):
            declaration_with_public_assets[field] = rewrite_supplier_asset_path(
                declaration_with_public_assets[field],
                current_app.config['DM_ASSETS_URL'])
    # Enhance question_content with any nested questions
    for question in chain.from_iterable(section.questions for section in declaration_sections):
        if question.type == 'multiquestion':
            for q in question.questions:
                question_content[q.id] = q

    return render_template(
        "suppliers/view_declaration.html",
        supplier=supplier,
        framework=framework,
        supplier_framework=sf,
        declaration=declaration_with_public_assets,
        content=content,
        question_content=question_content
    )


@main.route('/suppliers/<int:supplier_id>/agreements/<framework_slug>', methods=['GET'])
@role_required('admin-ccs-category', 'admin-ccs-sourcing', 'admin-framework-manager', 'admin-ccs-data-controller')
def view_signed_agreement(supplier_id, framework_slug):
    # not properly validating this - all we do is pass it through
    next_status = request.args.get("next_status")

    supplier = data_api_client.get_supplier(supplier_id)['suppliers']
    framework = data_api_client.get_framework(framework_slug)['frameworks']
    if not framework.get('frameworkAgreementVersion'):
        abort(404)
    supplier_framework = data_api_client.get_supplier_framework_info(supplier_id, framework_slug)['frameworkInterest']
    if not supplier_framework.get('agreementReturned'):
        abort(404)

    lot_names = []
    if framework["status"] in ("live", "expired"):
        # If the framework is live or expired we don't need to filter drafts, we only care about successful services
        service_iterator = data_api_client.find_services_iter(supplier_id=supplier_id, framework=framework_slug)
        for service in service_iterator:
            if service['lotName'] not in lot_names:
                lot_names.append(service['lotName'])
    else:
        # If the framework has not yet become live we need to filter out unsuccessful services
        service_iterator = data_api_client.find_draft_services_iter(supplier_id=supplier_id, framework=framework_slug)
        for service in service_iterator:
            if service["status"] == "submitted" and service['lotName'] not in lot_names:
                lot_names.append(service['lotName'])

    agreements_bucket = s3.S3(
        current_app.config['DM_AGREEMENTS_BUCKET'], endpoint_url=current_app.config.get("DM_S3_ENDPOINT_URL")
    )

    is_e_signature_flow = framework['isESignatureSupported']
    if not is_e_signature_flow:
        # Fetch path to supplier's signature page
        path = supplier_framework['agreementPath']
        template = "suppliers/view_signed_agreement.html"
    else:
        # Fetch path to combined countersigned agreement, if available
        path = supplier_framework.get('countersignedPath')
        template = "suppliers/view_esignature_agreement.html"

    url = get_signed_url(agreements_bucket, path, current_app.config['DM_ASSETS_URL']) if path else ""
    agreement_ext = get_extension(path) if path else ""

    if not url:
        current_app.logger.info(f'No agreement file found for {path}')
    return render_template(
        template,
        company_details=get_company_details_from_supplier(supplier),
        supplier=supplier,
        framework=framework,
        supplier_framework=supplier_framework,
        lot_names=list(sorted(lot_names)),
        agreement_url=url,
        agreement_ext=agreement_ext,
        next_status=next_status,
        is_e_signature_flow=is_e_signature_flow
    )


@main.route('/suppliers/agreements/<agreement_id>/on-hold', methods=['POST'])
@role_required('admin-ccs-sourcing')
def put_signed_agreement_on_hold(agreement_id):
    # not properly validating this - all we do is pass it through
    next_status = request.args.get("next_status")

    agreement = data_api_client.put_signed_agreement_on_hold(agreement_id, current_user.email_address)["agreement"]

    flash(AGREEMENT_ON_HOLD_MESSAGE.format(organisation_name=request.form['nameOfOrganisation']))

    return redirect(url_for(
        '.next_agreement',
        framework_slug=agreement["frameworkSlug"],
        supplier_id=agreement["supplierId"],
        status=next_status,
    ))


@main.route('/suppliers/agreements/<agreement_id>/approve', methods=['POST'])
@role_required('admin-ccs-sourcing')
def approve_agreement_for_countersignature(agreement_id):
    # not properly validating this - all we do is pass it through
    next_status = request.args.get("next_status")

    agreement = data_api_client.approve_agreement_for_countersignature(
        agreement_id,
        current_user.email_address,
        current_user.id,
    )["agreement"]

    flash(AGREEMENT_APPROVED_MESSAGE.format(organisation_name=request.form['nameOfOrganisation']))

    return redirect(url_for(
        '.next_agreement',
        framework_slug=agreement["frameworkSlug"],
        supplier_id=agreement["supplierId"],
        status=next_status,
    ))


@main.route('/suppliers/agreements/<agreement_id>/unapprove', methods=['POST'])
@role_required('admin-ccs-sourcing')
def unapprove_agreement_for_countersignature(agreement_id):
    # not properly validating this - all we do is pass it through
    next_status = request.args.get("next_status")

    agreement = data_api_client.unapprove_agreement_for_countersignature(
        agreement_id,
        current_user.email_address,
        current_user.id,
    )["agreement"]

    flash(AGREEMENT_APPROVAL_CANCELLED_MESSAGE.format(organisation_name=request.form['nameOfOrganisation']))

    return redirect(url_for(
        '.view_signed_agreement',
        framework_slug=agreement["frameworkSlug"],
        supplier_id=agreement["supplierId"],
        next_status=next_status,
    ))


@main.route('/suppliers/<int:supplier_id>/agreement/<framework_slug>', methods=['GET'])
@role_required('admin-ccs-category', 'admin-ccs-sourcing', 'admin-framework-manager', 'admin-ccs-data-controller')
def download_signed_agreement_file(supplier_id, framework_slug):
    # This route is used for pre-G-Cloud-8 agreement document downloads
    supplier_framework = data_api_client.get_supplier_framework_info(supplier_id, framework_slug)['frameworkInterest']
    if supplier_framework is None or not supplier_framework.get("agreementPath"):
        abort(404)
    document_name = degenerate_document_path_and_return_doc_name(supplier_framework['agreementPath'])
    return download_agreement_file(supplier_id, framework_slug, document_name)


@main.route('/suppliers/<int:supplier_id>/agreements/<framework_slug>/<document_name>', methods=['GET'])
@role_required('admin-ccs-category', 'admin-ccs-sourcing', 'admin-framework-manager', 'admin-ccs-data-controller')
def download_agreement_file(supplier_id, framework_slug, document_name):
    supplier_framework = data_api_client.get_supplier_framework_info(supplier_id, framework_slug)['frameworkInterest']
    if supplier_framework is None or not supplier_framework.get("declaration"):
        abort(404)

    agreements_bucket = s3.S3(
        current_app.config['DM_AGREEMENTS_BUCKET'], endpoint_url=current_app.config.get("DM_S3_ENDPOINT_URL")
    )
    path = get_document_path(framework_slug, supplier_id, 'agreements', document_name)
    url = get_signed_url(agreements_bucket, path, current_app.config['DM_ASSETS_URL'])
    if not url:
        abort(404)

    return redirect(url)


@main.route('/suppliers/<int:supplier_id>/countersigned-agreements/<framework_slug>', methods=['GET'])
@role_required('admin-ccs-sourcing')
def list_countersigned_agreement_file(supplier_id, framework_slug):
    supplier = data_api_client.get_supplier(supplier_id)['suppliers']
    framework = data_api_client.get_framework(framework_slug)['frameworks']
    supplier_framework = data_api_client.get_supplier_framework_info(supplier_id, framework_slug)['frameworkInterest']
    if not supplier_framework['onFramework'] or supplier_framework['agreementStatus'] in (None, 'draft'):
        abort(404)
    agreements_bucket = s3.S3(
        current_app.config['DM_AGREEMENTS_BUCKET'], endpoint_url=current_app.config.get("DM_S3_ENDPOINT_URL")
    )
    countersigned_agreement_document = agreements_bucket.get_key(supplier_framework.get('countersignedPath'))

    remove_countersigned_agreement_confirm = convert_to_boolean(request.args.get('remove_countersigned_agreement'))

    countersigned_agreement = []
    if countersigned_agreement_document:
        last_modified = datetimeformat(parse_date(countersigned_agreement_document['last_modified']))
        document_name = degenerate_document_path_and_return_doc_name(supplier_framework.get('countersignedPath'))
        countersigned_agreement = [{"last_modified": last_modified, "document_name": document_name}]

    return render_template(
        "suppliers/upload_countersigned_agreement.html",
        supplier=supplier,
        framework=framework,
        countersigned_agreement=countersigned_agreement,
        remove_countersigned_agreement_confirm=remove_countersigned_agreement_confirm
    )


@main.route('/suppliers/<int:supplier_id>/countersigned-agreements/<framework_slug>', methods=['POST'])
@role_required('admin-ccs-sourcing')
def upload_countersigned_agreement_file(supplier_id, framework_slug):
    supplier_framework = data_api_client.get_supplier_framework_info(supplier_id, framework_slug)['frameworkInterest']
    if not supplier_framework['onFramework'] or supplier_framework['agreementStatus'] in (None, 'draft'):
        abort(404)
    agreement_id = supplier_framework['agreementId']
    agreements_bucket = s3.S3(
        current_app.config['DM_AGREEMENTS_BUCKET'], endpoint_url=current_app.config.get("DM_S3_ENDPOINT_URL")
    )
    errors = {}

    if request.files.get('countersigned_agreement'):
        the_file = request.files['countersigned_agreement']
        if not file_is_pdf(the_file):
            errors['countersigned_agreement'] = 'not_pdf'
            flash(COUNTERSIGNED_AGREEMENT_NOT_PDF_MESSAGE)

        if 'countersigned_agreement' not in errors.keys():
            supplier_name = supplier_framework.get('declaration', {}).get('nameOfOrganisation')
            if not supplier_name:
                supplier_name = data_api_client.get_supplier(supplier_id)['suppliers']['name']
            if supplier_framework['agreementStatus'] not in ['approved', 'countersigned']:
                data_api_client.approve_agreement_for_countersignature(
                    agreement_id,
                    current_user.email_address,
                    current_user.id
                )

            path = generate_timestamped_document_upload_path(
                framework_slug, supplier_id, 'agreements', COUNTERPART_FILENAME
            )
            download_filename = generate_download_filename(supplier_id, COUNTERPART_FILENAME, supplier_name)
            agreements_bucket.save(
                path, the_file, acl='bucket-owner-full-control', move_prefix=None, download_filename=download_filename
            )

            data_api_client.update_framework_agreement(
                agreement_id,
                {"countersignedAgreementPath": path},
                current_user.email_address
            )

            data_api_client.create_audit_event(
                audit_type=AuditTypes.upload_countersigned_agreement,
                user=current_user.email_address,
                object_type='suppliers',
                object_id=supplier_id,
                data={'upload_countersigned_agreement': path})

            flash(UPLOAD_COUNTERSIGNED_AGREEMENT_MESSAGE)

    return redirect(url_for(
        '.list_countersigned_agreement_file',
        supplier_id=supplier_id,
        framework_slug=framework_slug)
    )


@main.route('/suppliers/<int:supplier_id>/countersigned-agreements-remove/<framework_slug>',
            methods=['GET', 'POST'])
@role_required('admin-ccs-sourcing')
def remove_countersigned_agreement_file(supplier_id, framework_slug):
    supplier_framework = data_api_client.get_supplier_framework_info(supplier_id, framework_slug)['frameworkInterest']
    document = supplier_framework.get('countersignedPath')
    agreements_bucket = s3.S3(
        current_app.config['DM_AGREEMENTS_BUCKET'], endpoint_url=current_app.config.get("DM_S3_ENDPOINT_URL")
    )

    if request.method == 'GET':
        return redirect(url_for(
            '.list_countersigned_agreement_file',
            supplier_id=supplier_id,
            framework_slug=framework_slug) + "?remove_countersigned_agreement=true"
        )

    if request.method == 'POST':
        # Remove path first - as we don't want path to exist in DB with no corresponding file in S3
        # But an orphaned file in S3 wouldn't be so bad
        data_api_client.update_framework_agreement(
            supplier_framework['agreementId'],
            {"countersignedAgreementPath": None},
            current_user.email_address
        )
        agreements_bucket.delete_key(document)

        data_api_client.create_audit_event(
            audit_type=AuditTypes.delete_countersigned_agreement,
            user=current_user.email_address,
            object_type='suppliers',
            object_id=supplier_id,
            data={'upload_countersigned_agreement': document})

    return redirect(url_for(
        '.list_countersigned_agreement_file',
        supplier_id=supplier_id,
        framework_slug=framework_slug)
    )


@main.route(
    '/suppliers/<int:supplier_id>/edit/declarations/<string:framework_slug>/<string:section_id>',
    methods=['GET'])
@role_required('admin-ccs-sourcing')
def edit_supplier_declaration_section(supplier_id, framework_slug, section_id):
    supplier = data_api_client.get_supplier(supplier_id)['suppliers']
    framework = data_api_client.get_framework(framework_slug)['frameworks']
    if framework['status'] not in ['pending', 'standstill', 'live']:
        abort(403)
    try:
        declaration = data_api_client.get_supplier_declaration(supplier_id, framework_slug)['declaration']
    except APIError as e:
        if e.status_code != 404:
            raise
        declaration = {}

    content = content_loader.get_manifest(framework_slug, 'declaration').filter(declaration, inplace_allowed=True)
    section = content.get_section(section_id)
    if section is None:
        abort(404)

    return render_template(
        "suppliers/edit_declaration.html",
        supplier=supplier,
        framework=framework,
        declaration=declaration,
        section=section
    )


@main.route(
    '/suppliers/<int:supplier_id>/edit/declarations/<string:framework_slug>/<string:section_id>',
    methods=['POST'])
@role_required('admin-ccs-sourcing')
def update_supplier_declaration_section(supplier_id, framework_slug, section_id):
    # Supplier must exist.
    data_api_client.get_supplier(supplier_id)
    framework = data_api_client.get_framework(framework_slug)['frameworks']
    if framework['status'] not in ['pending', 'standstill', 'live']:
        abort(403)
    try:
        declaration = data_api_client.get_supplier_declaration(supplier_id, framework_slug)['declaration']
    except APIError as e:
        if e.status_code != 404:
            raise
        declaration = {}

    content = content_loader.get_manifest(framework_slug, 'declaration').filter(declaration, inplace_allowed=True)
    section = content.get_section(section_id)
    if section is None:
        abort(404)

    posted_data = section.get_data(request.form)

    if section.has_changes_to_save(declaration, posted_data):
        declaration.update(posted_data)
        data_api_client.set_supplier_declaration(
            supplier_id, framework_slug, declaration,
            current_user.email_address)

    return redirect(url_for('.view_supplier_declaration',
                            supplier_id=supplier_id, framework_slug=framework_slug))


@main.route('/suppliers/users', methods=['GET'])
@role_required('admin', 'admin-ccs-category', 'admin-framework-manager', 'admin-ccs-data-controller')
def find_supplier_users():

    if not request.args.get('supplier_id'):
        abort(404)

    supplier = data_api_client.get_supplier(request.args['supplier_id'])
    users = data_api_client.find_users_iter(request.args.get("supplier_id"))

    return render_template(
        "view_supplier_users.html",
        users=users,
        invite_form=EmailAddressForm(),
        move_user_form=MoveUserForm(),
        supplier=supplier["suppliers"]
    )


@main.route('/suppliers/users/<int:user_id>/unlock', methods=['POST'])
@role_required('admin', 'admin-ccs-category')
def unlock_user(user_id):
    user = data_api_client.update_user(user_id, locked=False, updater=current_user.email_address)
    if "source" in request.form:
        return redirect(request.form["source"])
    return redirect(url_for('.find_supplier_users', supplier_id=user['users']['supplier']['supplierId']))


@main.route('/suppliers/users/<int:user_id>/activate', methods=['POST'])
@role_required('admin', 'admin-ccs-category')
def activate_user(user_id):
    user = data_api_client.update_user(user_id, active=True, updater=current_user.email_address)
    if "source" in request.form:
        return redirect(request.form["source"])
    return redirect(url_for('.find_supplier_users', supplier_id=user['users']['supplier']['supplierId']))


@main.route('/suppliers/users/<int:user_id>/deactivate', methods=['POST'])
@role_required('admin', 'admin-ccs-category')
def deactivate_user(user_id):
    user = data_api_client.update_user(user_id, active=False, updater=current_user.email_address)
    if "source" in request.form:
        return redirect(request.form["source"])
    return redirect(url_for('.find_supplier_users', supplier_id=user['users']['supplier']['supplierId']))


@main.route('/suppliers/<int:supplier_id>/move-existing-user', methods=['POST'])
@role_required('admin', 'admin-ccs-category')
def move_user_to_new_supplier(supplier_id):
    move_user_form = MoveUserForm()

    try:
        suppliers = data_api_client.get_supplier(supplier_id)
        users = data_api_client.find_users_iter(supplier_id)
    except HTTPError as e:
        current_app.logger.error(str(e), supplier_id)
        if e.status_code != 404:
            raise
        else:
            abort(404, "Supplier not found")

    if move_user_form.validate_on_submit():
        try:
            user = data_api_client.get_user(email_address=move_user_form.user_to_move_email_address.data)
        except HTTPError as e:
            current_app.logger.error(str(e), supplier_id)
            raise

        if user:
            data_api_client.update_user(
                user['users']['id'],
                role='supplier',
                supplier_id=supplier_id,
                active=True,
                updater=current_user.email_address
            )
            flash(SUPPLIER_USER_MESSAGES["user_moved"])
        else:
            flash(SUPPLIER_USER_MESSAGES["user_not_moved"], "error")
        return redirect(url_for('.find_supplier_users', supplier_id=supplier_id))
    else:
        return render_template(
            "view_supplier_users.html",
            invite_form=EmailAddressForm(),
            move_user_form=move_user_form,
            users=users,
            supplier=suppliers["suppliers"]
        ), 400


@main.route('/suppliers/<int:supplier_id>/services', methods=['GET'])
@role_required('admin', 'admin-ccs-category', 'admin-framework-manager', 'admin-ccs-data-controller')
def find_supplier_services(supplier_id):
    remove_services_for_framework_slug = request.args.get('remove')
    publish_services_for_framework_slug = request.args.get('publish')

    frameworks = data_api_client.find_frameworks()['frameworks']
    supplier = data_api_client.get_supplier(supplier_id)["suppliers"]

    frameworks_services = {
        framework_slug: list(framework_services)
        for framework_slug, framework_services in
        groupby(sorted(data_api_client.find_services_iter(
            supplier_id=supplier_id,
            framework=','.join(f['slug'] for f in frameworks if f['status'] in ['live', 'expired'])
        ), key=itemgetter('frameworkSlug')), key=itemgetter('frameworkSlug'))
    }

    remove_services_for_framework, publish_services_for_framework = None, None

    if remove_services_for_framework_slug:
        if remove_services_for_framework_slug not in frameworks_services:
            abort(400, 'No services for framework')
        if not any(i['status'] == 'published' for i in frameworks_services[remove_services_for_framework_slug]):
            abort(400, 'No published services on framework')

        remove_services_for_framework = next(filter(
            lambda i: i['slug'] == remove_services_for_framework_slug,
            frameworks
        ))
    elif publish_services_for_framework_slug:
        if publish_services_for_framework_slug not in frameworks_services:
            abort(400, 'No services for framework')
        if not any(i['status'] == 'disabled' for i in frameworks_services[publish_services_for_framework_slug]):
            abort(400, 'No suspended services on framework')

        publish_services_for_framework = next(filter(
            lambda i: i['slug'] == publish_services_for_framework_slug,
            frameworks
        ))

    return render_template(
        'view_supplier_services.html',
        frameworks=frameworks,
        frameworks_services=frameworks_services,
        supplier=supplier,
        remove_services_for_framework=remove_services_for_framework,
        publish_services_for_framework=publish_services_for_framework,
    )


@main.route('/suppliers/<int:supplier_id>/services', methods=['POST'])
@role_required('admin-ccs-category')
def toggle_supplier_services(supplier_id):
    remove_services = request.args.get('remove')
    publish_services = request.args.get('publish')

    toggle_action = {
        'framework_slug': remove_services or publish_services,
        'old_status': 'published' if remove_services else 'disabled',
        'new_status': 'disabled' if remove_services else 'published',
        'flash_message': SUPPLIER_SERVICES_REMOVED_MESSAGE if remove_services else SUPPLIER_SERVICES_UNSUSPENDED_MESSAGE
    }
    if not toggle_action['framework_slug']:
        abort(400, 'Invalid framework')

    framework = data_api_client.get_framework(toggle_action['framework_slug'])['frameworks']
    if framework['status'] != 'live':
        abort(400, "Cannot toggle services for framework {}".format(toggle_action['framework_slug']))

    services = tuple(data_api_client.find_services_iter(
        supplier_id=supplier_id,
        framework=toggle_action['framework_slug'],
        status=toggle_action['old_status']
    ))
    if not services:
        abort(400, 'No {} services on framework'.format(toggle_action['old_status']))

    for service in services:
        data_api_client.update_service_status(
            service['id'],
            toggle_action['new_status'],
            current_user.email_address,
            wait_for_index=False,
        )

    flash(
        " ".join((
            toggle_action['flash_message'].format(
                supplier_name=services[0]['supplierName'],
                framework_name=services[0]['frameworkName']
            ),
            SUPPLIER_SERVICES_DELAYED_INDEX_MESSAGE,
        ))
    )
    return redirect(url_for('.find_supplier_services', supplier_id=supplier_id))


def _draft_services_annotated_unanswered_counts(framework_slug, draft_services):
    try:
        manifest = content_loader.get_manifest(framework_slug, "edit_service_as_admin")
    except ContentNotFoundError:
        return None

    return tuple(
        {
            **draft_service,
            "unansweredRequiredCount": count_unanswered_questions(
                manifest.filter(draft_service).summary(draft_service)
            )[0],
        } for draft_service in draft_services
    )


@main.route('/suppliers/<int:supplier_id>/draft-services', methods=['GET'])
@role_required('admin-framework-manager', 'admin-ccs-sourcing')
def find_supplier_draft_services(supplier_id):
    supplier = data_api_client.get_supplier(supplier_id)["suppliers"]
    frameworks = data_api_client.find_frameworks()['frameworks']

    if current_user.has_role('admin-ccs-sourcing'):
        visible_framework_statuses = ["pending", "standstill", "live", "expired"]
    elif current_user.has_role('admin-framework-manager'):
        visible_framework_statuses = ["open", "pending", "standstill", "live", "expired"]
    else:
        # No other roles can access this page yet, but we might want to add them later
        visible_framework_statuses = []

    frameworks = (fw for fw in frameworks if fw['status'] in visible_framework_statuses)

    frameworks_draft_services = {
        framework_slug: draft_services
        for framework_slug, draft_services in (
            (framework_slug, _draft_services_annotated_unanswered_counts(framework_slug, draft_services),)
            for framework_slug, draft_services in
            groupby(sorted(
                data_api_client.find_draft_services_iter(supplier_id),
                key=lambda draft_service: (draft_service["frameworkSlug"], draft_service["createdAt"]),
            ), key=lambda draft_service: draft_service["frameworkSlug"])
        ) if draft_services is not None  # omit frameworks for which we couldn't retrieve the manifest
    }

    return render_template(
        'view_supplier_draft_services.html',
        frameworks=frameworks,
        frameworks_draft_services=frameworks_draft_services,
        supplier=supplier,
    )


@main.route('/suppliers/<int:supplier_id>/invite-user', methods=['POST'])
@role_required('admin', 'admin-ccs-category')
def invite_user(supplier_id):
    invite_form = EmailAddressForm()

    try:
        suppliers = data_api_client.get_supplier(supplier_id)
        users = data_api_client.find_users_iter(supplier_id)
    except HTTPError as e:
        current_app.logger.error(str(e), supplier_id)
        if e.status_code != 404:
            raise
        else:
            abort(404, "Supplier not found")

    if invite_form.validate_on_submit():
        send_user_account_email(
            'supplier',
            invite_form.email_address.data,
            current_app.config['NOTIFY_TEMPLATES']['invite_contributor'],
            extra_token_data={
                'supplier_id': supplier_id,
                'supplier_name': suppliers['suppliers']['name']
            },
            personalisation={
                'user': 'The Digital Marketplace team',
                'supplier': suppliers['suppliers']['name']
            }
        )

        data_api_client.create_audit_event(
            audit_type=AuditTypes.invite_user,
            user=current_user.email_address,
            object_type='suppliers',
            object_id=supplier_id,
            data={'invitedEmail': invite_form.email_address.data})

        flash(SUPPLIER_USER_MESSAGES['user_invited'])
        return redirect(url_for('.find_supplier_users', supplier_id=supplier_id))
    else:
        return render_template(
            "view_supplier_users.html",
            invite_form=invite_form,
            move_user_form=MoveUserForm(),
            users=users,
            supplier=suppliers["suppliers"]
        ), 400
