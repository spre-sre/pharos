"""Regression: exact-name KubeArchive queries return the resource, not 0 results.

An exact name (no wildcard) makes KubeArchiveClient.query_resources() GET the
single-resource URL, which returns a bare object (kind: PipelineRun) instead of
a List. query_kubearchive_resources() used to read only data['items'] and
silently dropped the result ("No archived resources found" for a resource the
same tool had just listed).
"""

import sys
from pathlib import Path
from unittest.mock import AsyncMock

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent / "src"))

from helpers.kubearchive_integration import query_kubearchive_resources


def _client_returning(data):
    client = AsyncMock()
    client.query_resources = AsyncMock(
        return_value={'status': 'success', 'data': data, 'source': 'kubearchive'}
    )
    return client


_SINGLE = {
    'kind': 'PipelineRun',
    'apiVersion': 'tekton.dev/v1',
    'metadata': {
        'name': 'keycloak-user-service-on-push-75fct',
        'namespace': 'hcc-fr-tenant',
        'creationTimestamp': '2026-08-20T08:50:21Z',
    },
    'status': {'conditions': [{'type': 'Succeeded', 'status': 'True', 'reason': 'Succeeded'}]},
}


@pytest.mark.asyncio
async def test_exact_name_single_object_response_yields_one_resource():
    result = await query_kubearchive_resources(
        kubearchive_client=_client_returning(_SINGLE),
        resource_type='pipelinerun',
        namespace='hcc-fr-tenant',
        name='keycloak-user-service-on-push-75fct',
    )
    assert result['kubearchive_status'] == 'success'
    assert result['total_count'] == 1
    assert result['resources'][0]['name'] == 'keycloak-user-service-on-push-75fct'


@pytest.mark.asyncio
async def test_list_response_still_extracts_items():
    result = await query_kubearchive_resources(
        kubearchive_client=_client_returning({'kind': 'List', 'items': [_SINGLE, _SINGLE]}),
        resource_type='pipelinerun',
        namespace='hcc-fr-tenant',
    )
    assert result['kubearchive_status'] == 'success'
    assert result['total_count'] == 2


@pytest.mark.asyncio
async def test_empty_or_unrecognized_data_yields_zero_resources():
    for data in ({}, {'kind': 'List', 'items': []}, {'metadata': {}}):
        result = await query_kubearchive_resources(
            kubearchive_client=_client_returning(data),
            resource_type='pipelinerun',
            namespace='hcc-fr-tenant',
        )
        assert result['kubearchive_status'] == 'success'
        assert result['total_count'] == 0
