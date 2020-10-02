#
# Copyright 2020 Red Hat, Inc.
#
# This program is free software: you can redistribute it and/or modify
# it under the terms of the GNU Affero General Public License as
# published by the Free Software Foundation, either version 3 of the
# License, or (at your option) any later version.
#
# This program is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
# GNU Affero General Public License for more details.
#
# You should have received a copy of the GNU Affero General Public License
# along with this program.  If not, see <https://www.gnu.org/licenses/>.
#

"""View for internal tenant management."""
import datetime
import json
import logging

import pytz
from django.conf import settings
from django.db.migrations.recorder import MigrationRecorder
from django.http import HttpResponse
from django.shortcuts import get_object_or_404
from management.models import Group, Role
from management.tasks import run_migrations_in_worker
from tenant_schemas.utils import tenant_context

from api.models import Tenant


logger = logging.getLogger(__name__)


def destructive_ok():
    """Determine if it's ok to run destructive operations."""
    now = datetime.datetime.utcnow().replace(tzinfo=pytz.UTC)
    return now < settings.INTERNAL_DESTRUCTIVE_API_OK_UNTIL


def tenant_is_unmodified():
    """Determine whether or not the tenant has been modified."""
    if Role.objects.filter(system=True).count() != Role.objects.count():
        return False
    if Group.objects.count() != 1:
        return False
    if Group.objects.filter(system=True).count() != 1:
        return False
    return True


def list_unmodified_tenants(request):
    """List unmodified tenants."""
    logger.info(f"Unmodified tenants requested by: {request.user.username}")
    tenant_qs = Tenant.objects.exclude(schema_name="public")
    to_return = []
    for tenant_obj in tenant_qs:
        with tenant_context(tenant_obj):
            if tenant_is_unmodified():
                to_return.append(tenant_obj.schema_name)
    payload = {
        "unmodified_tenants": to_return,
        "unmodified_tenants_count": len(to_return),
        "total_tenants_count": tenant_qs.count(),
    }
    return HttpResponse(json.dumps(payload), content_type="application/json")


def tenant_view(request, tenant_schema_name):
    """View method for internal tenant requests."""
    logger.info(f"Tenant view: {request.method} {request.user.username}")
    if request.method == "DELETE":
        if not destructive_ok():
            return HttpResponse("Destructive operations disallowed.", status=400)

        tenant_obj = get_object_or_404(Tenant, schema_name=tenant_schema_name)
        with tenant_context(tenant_obj):
            if tenant_is_unmodified():
                logger.warning(f"Deleting tenant {tenant_schema_name}. Requested by {request.user.username}")
                tenant_obj.delete()
                return HttpResponse(status=204)
            else:
                return HttpResponse("Tenant cannot be deleted.", status=400)
    return HttpResponse(status=405)


def run_migrations(request):
    """View method for running migrations."""
    if request.method == "POST":
        logger.info(f"Running migrations: {request.method} {request.user.username}")
        run_migrations_in_worker.delay()
        return HttpResponse("Migrations are running in a background worker.", status=202)
    return HttpResponse(status=405)


def migration_progress(request):
    """View method for checking migration progress."""
    if request.method == "GET":
        migration_name = request.GET.get("migration_name")
        app = request.GET.get("app", "management")
        if not migration_name:
            return HttpResponse("Please specify a migration name in the `?migration_name=` param.", status=400)
        tenants_completed = 0
        tenant_qs = Tenant.objects.exclude(schema_name="public")
        tenant_count = tenant_qs.count()
        for idx, tenant in enumerate(list(tenant_qs)):
            with tenant_context(tenant):
                migrations_have_run = MigrationRecorder.Migration.objects.filter(name=migration_name, app=app).exists()
                if migrations_have_run:
                    tenants_completed += 1
        payload = {
            "migration_name": migration_name,
            "tenants_completed": tenants_completed,
            "total_tenants_count": tenant_count,
            "percent_completed": int((tenants_completed / tenant_count) * 100),
        }

        return HttpResponse(json.dumps(payload), status=200)
    return HttpResponse(status=405)
