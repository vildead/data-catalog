#  DataCatalog
#  Copyright (C) 2020  University of Luxembourg
#
#  This program is free software: you can redistribute it and/or modify
#  it under the terms of the GNU Affero General Public License as
#  published by the Free Software Foundation, either version 3 of the
#  License, or (at your option) any later version.
#
#  This program is distributed in the hope that it will be useful,
#  but WITHOUT ANY WARRANTY; without even the implied warranty of
#  MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
#  GNU Affero General Public License for more details.
#
#  You should have received a copy of the GNU Affero General Public License
#  along with this program.  If not, see <https://www.gnu.org/licenses/>.

"""
    datacatalog.web_controllers
    -------------------

    HTML endpoints:
        - entities_search
        - search
        - entity_details
        - about
        - request_access
"""

from typing import List

from flask import render_template, flash, redirect, url_for, request, Response, Request
from flask_login import current_user
from flask_mail import Message
from flask_wtf.csrf import CSRFError

from .. import mail, login_manager
from ..exceptions import SolrQueryException
from ..forms.request_access_form import RequestAccess
from ..models import *
from ..solr.solr_orm_entity import SolrEntity
from ..pagination import Pagination

logger = app.logger
RESULTS_PER_PAGE = 5
FAIR_VALUES = app.config.get('FAIR_VALUES')
FAIR_VALUES_SHOW = app.config.get('FAIR_VALUES_SHOW')
FAIR_EVALUATIONS_SHOW = app.config.get('FAIR_EVALUATIONS_SHOW')


# errors handlers

@app.errorhandler(CSRFError)
def csrf_error(reason) -> Response:
    explanation = "The session might have timed out, try to go back and refresh the page before doing any action"
    return render_template('error.html', message="Error 400 - " + reason,
                           explanation=explanation), 400


@app.errorhandler(404)
def page_not_found(e) -> Response:
    """
    Customize the 404 pages
    @param e: the exception that triggered the 404
    @type e: Exception
    @return: a custom 404 page
    @rtype:  str
    """
    app.logger.error(e)
    return render_template('error.html', message="Error 404 - Page not found", show_home_link=True), 404


@app.route('/<entity_name>s', methods=['GET'])
@app.cache.cached(timeout=0)
def entities_search(entity_name: str) -> Response:
    """
    Generic search endpoint for any entity
    @param entity_name:  the name of the entity we want to browse/search
    @return: html page showing the search results and facets
    """
    entity_class = app.config['entities'][entity_name]
    exporter = getattr(app, 'excel_exporter', None)
    return default_search(request, exporter=exporter, entity=entity_class, template='search_' + entity_name + '.html')


def make_key():
    return request.full_path


@app.route('/', methods=['GET'])
def search() -> Response:
    """
    Search view for default entity, home page
    @return: html page showing the search results and facets for default entity
    """
    exporter = getattr(app, 'excel_exporter', None)
    default_entity = app.config['entities'][app.config.get('DEFAULT_ENTITY', 'dataset')]
    return default_search(request, exporter=exporter, entity=default_entity)


def default_search(search_request: Request, extra_filter: List[str] = None, template: str = None,
                   facets_order: List[str] = None,
                   results_per_page: int = None,
                   exporter: object = None, entity: SolrEntity = None) -> Response:
    """
    Compute search results and render the search template
    @param search_request: the flask request object
    @param extra_filter: can be used to apply extra filters on the search, example ['status': 'completed']
    @param template: allows overriding the template used: default is search_`entity_name`.html
    @param facets_order: to override the default facets
    @param results_per_page: number of results per page
    @param exporter: to allow custom exporter (e.g. excel exporter)
    @param entity: the entity class
    @return: HTML page showing search results
    """
    query = search_request.args.get('query', '').strip()

    searcher = entity.query
    entity_type = searcher.entity_name
    if template is None:
        template = 'search_' + entity_type + '.html'
    searcher_default_sort, searcher_default_sort_order = searcher.get_default_sort(query)
    page = search_request.args.get('page', '1').strip()
    sort_by = search_request.args.get('sort_by', searcher_default_sort)
    sort_order = search_request.args.get('order', searcher_default_sort_order)
    export_excel = 'export_excel' in search_request.args and exporter

    results_per_page = results_per_page or app.config.get('RESULTS_PER_PAGE', 20)
    sort_options, sort_labels = searcher.get_sort_options()
    try:
        page = int(page)
    except ValueError:
        page = 1
    if page < 1 or sort_order not in ['asc', 'desc'] or (sort_by and sort_by not in sort_options):
        return render_template("error.html", message="wrong parameters"), 400
    if export_excel:
        rows = 100000
        start = 0
    else:
        rows = results_per_page
        start = (page - 1) * results_per_page
    facets_order = facets_order or app.config.get('FACETS_ORDER', {}).get(entity_type, [])
    # only do facet when some records are present as solr triggers an error if not
    if searcher.count() > 0:
        facets = searcher.get_facets(facets_order)
        for facet in facets.values():
            if facet.field_name in search_request.args:
                values = search_request.args.getlist(facet.field_name)
                facet.set_values(values)
            else:
                facet.use_default()
    else:
        facets = {}
    try:
        fq = None
        if extra_filter:
            fq = [extra_filter]
        if entity_type:
            entity_filter = 'type:' + entity_type
            if not fq:
                fq = []
            fq.append(entity_filter)

        results = searcher.search(query, rows=rows, start=start,
                                  sort=sort_by, sort_order=sort_order, facets=facets.values(), fuzzy=True,
                                  fq=fq)
    except (NotImplementedError, SolrQueryException) as e:
        logger.error(str(e), exc_info=e)
        return render_template('error.html', message="a problem occurred while querying the indexer",
                               explanation="see log for more details")

    if export_excel:
        if not current_user.is_authenticated:
            return login_manager.unauthorized()
        excel_exporter = exporter()
        return excel_exporter.export_results_as_xlsx(entity_type, getattr(results, entity_type + 's'))
    pagination = Pagination(page, results_per_page, results.hits)
    ordered_facets = []
    for (attribute_name, label) in facets_order:
        facet = facets.get(attribute_name, None)
        if facet is not None:
            ordered_facets.append(facet)

    return render_template(template, results=results, pagination=pagination,
                           sort_options=sort_options, sort_labels=sort_labels, selected_sort=sort_by,
                           sort_order=sort_order, facets=ordered_facets,
                           fair_values=FAIR_VALUES, fair_values_show=FAIR_VALUES_SHOW,
                           fair_evaluations_show=FAIR_EVALUATIONS_SHOW)


@app.route('/e/<entity_name>/<entity_id>', methods=['GET'])
@app.cache.cached(timeout=0)
def entity_details(entity_name: str, entity_id: str) -> Response:
    """
    Show the detailed view of a specific entity
    Template used is `entity_name`.html
    @param entity_name: name of the entity class
    @param entity_id: id of the entity
    @return: HTML page
    """
    entity = get_entity(entity_name, entity_id)
    kwargs = {
        entity_name: entity
    }
    return render_template(entity_name + '.html', fair_evaluations_show=FAIR_EVALUATIONS_SHOW, **kwargs)


def get_entity(entity_name: str, entity_id: str) -> SolrEntity:
    """
    Retrieve an entity from Solr and create an instance of the corresponding Entity
    @param entity_name: name of the entity class
    @param entity_id: id of the entity
    @return: an instance of the corresponding entity or 404 if not found
    """
    entity_class = app.config['entities'][entity_name]
    entity = entity_class.query.get_or_404(entity_id)
    return entity


@app.route('/about', methods=['GET'])
@app.cache.cached(timeout=0)
def about() -> Response:
    """
    Static about page
    @return: HTML page
    """
    return render_template('about.html')


@app.route('/request_access/<entity_name>/<entity_id>', methods=['GET', 'POST'])
def request_access(entity_name: str, entity_id: str) -> Response:
    """
    Form to request access to an entity by email
    display the form for a GET request
    send an email and redirects to the home page for a POST request
    @param entity_name: the type of entity we want access to
    @param entity_id: the id of the dataset the user want to request to
    @return: redirects to / or form
    """
    form = RequestAccess(request.form)
    entity = get_entity(entity_name, entity_id)
    if request.method == 'POST':
        if not form.validate():

            if form.recaptcha.errors:
                flash('The Captcha response parameter is missing..', category="error")

            kwargs = {entity_name: entity}
            return render_template('request_access.html', form=form, **kwargs)
        else:
            subject = "Grant access to " + entity.title
            url = url_for('entity_details', entity_name=entity_name, entity_id=entity.id, _external=True)
            msg = Message(subject, sender=form.email.data, recipients=app.config['EMAIL_RECIPIENT'])

            msg.body = """
        From: %s <%s>
        
        %s

        %s
        """ % (form.name.data, form.email.data, form.message.data, url)
            mail.send(msg)
            flash("Email sent successfully.", category='success')
            return redirect('/')

    elif request.method == 'GET':
        kwargs = {entity_name: entity}
        return render_template('request_access.html', form=form, **kwargs)
