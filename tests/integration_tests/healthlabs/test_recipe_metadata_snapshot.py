"""Projection request snapshots reuse facts without sharing mutable recipe inputs."""

from contextlib import contextmanager
from datetime import UTC, datetime
from uuid import UUID, uuid4

import pytest
import sqlalchemy as sa
from sqlalchemy.orm import Session

from mealie.db.db_setup import SessionLocal, generate_session
from mealie.db.models.healthlabs import HealthLabsFoodMatch, HealthLabsNutrientFood
from mealie.services.healthlabs import recipe_metadata_service as metadata_module

FIXED_TIME = datetime(2026, 10, 30, 8, tzinfo=UTC)


@pytest.fixture
def shared_source_page(
    api_client, healthlabs_provision, healthlabs_recipe_food, healthlabs_nutrition_source, unfiltered_database
):
    identity, headers = healthlabs_provision()
    marker = f"snapshot{uuid4().hex}"
    name = f"rice {marker}"
    first, food_id = healthlabs_recipe_food(headers, name)
    _, source_id = healthlabs_nutrition_source(name=name)
    session = unfiltered_database.session
    session.add(
        HealthLabsFoodMatch(
            ingredient_food_id=UUID(food_id),
            nutrient_food_id=UUID(source_id),
            country="GB",
            match_method="manual",
            confidence=1,
            reviewed_by_user_id=UUID(identity["userId"]),
            reviewed_at=FIXED_TIME,
        )
    )
    session.commit()
    unit_id = first["ingredients"][0]["unit"]["id"]
    for index in range(1, 20):
        result = api_client.post(
            "/api/healthlabs/v1/recipes",
            headers={**headers, "Idempotency-Key": str(uuid4())},
            json={
                "name": f"{marker} {index}",
                "servings": 1,
                "ingredients": [
                    {
                        "quantity": 100 + index,
                        "food": {"id": food_id, "name": name},
                        "unit": {"id": unit_id, "name": "gram", "abbreviation": "g"},
                    }
                ],
            },
        )
        assert result.status_code == 201, result.text
    return headers, marker, UUID(source_id)


def _postgres_engine():
    engine = SessionLocal.kw["bind"]
    if engine.dialect.name != "postgresql":
        pytest.skip("real PostgreSQL is required for the request snapshot branch")
    return engine


@contextmanager
def _candidate_queries(engine):
    statements = []

    def record(connection, cursor, statement, parameters, context, executemany):
        if "lower(healthlabs_nutrient_foods.name)" in statement and "LIKE" in statement:
            statements.append(statement)

    # Source reads use a dedicated engine, so observe every engine connection.
    sa.event.listen(sa.engine.Engine, "before_cursor_execute", record)
    try:
        yield statements
    finally:
        sa.event.remove(sa.engine.Engine, "before_cursor_execute", record)


def test_twenty_recipe_page_uses_one_source_scan_and_preserves_every_quantity(
    api_client, shared_source_page, monkeypatch
):
    engine = _postgres_engine()
    headers, marker, _ = shared_source_page
    monkeypatch.setattr(metadata_module, "get_utc_now", lambda: FIXED_TIME)
    with _candidate_queries(engine) as statements:
        result = api_client.get("/api/healthlabs/v1/recipes", headers=headers, params={"q": marker})
    assert result.status_code == 200, result.text
    body = result.json()
    assert len(body["items"]) == 20
    assert len(statements) == 1
    for recipe in body["items"]:
        quantity = recipe["ingredients"][0]["quantity"]
        assert recipe["nutritionMetadata"]["perServing"]["proteinG"]["verified"] == pytest.approx(quantity / 10)


def test_source_commit_mid_page_is_visible_only_to_the_next_request(api_client, shared_source_page, monkeypatch):
    engine = _postgres_engine()
    headers, marker, source_id = shared_source_page
    monkeypatch.setattr(metadata_module, "get_utc_now", lambda: FIXED_TIME)
    original = metadata_module.RecipeMetadataService.inputs
    changed = False

    def inputs(service, recipe):
        nonlocal changed
        result = original(service, recipe)
        if not changed:
            changed = True
            with Session(engine) as writer:
                source = writer.get(HealthLabsNutrientFood, source_id)
                source.nutrient_values = {**source.nutrient_values, "protein_g": {"amount": 20, "unit": "g"}}
                writer.commit()
        return result

    with monkeypatch.context() as patch:
        patch.setattr(metadata_module.RecipeMetadataService, "inputs", inputs)
        before = api_client.get("/api/healthlabs/v1/recipes", headers=headers, params={"q": marker})
    assert before.status_code == 200, before.text
    for recipe in before.json()["items"]:
        quantity = recipe["ingredients"][0]["quantity"]
        assert recipe["nutritionMetadata"]["perServing"]["proteinG"]["verified"] == pytest.approx(quantity / 10)
    after = api_client.get("/api/healthlabs/v1/recipes", headers=headers, params={"q": marker})
    assert after.status_code == 200, after.text
    before_by_id = {recipe["id"]: recipe for recipe in before.json()["items"]}
    for recipe in after.json()["items"]:
        quantity = recipe["ingredients"][0]["quantity"]
        assert recipe["nutritionMetadata"]["perServing"]["proteinG"]["verified"] == pytest.approx(quantity / 5)
        assert (
            recipe["nutritionMetadata"]["inputFingerprint"]
            != before_by_id[recipe["id"]]["nutritionMetadata"]["inputFingerprint"]
        )


def test_cached_page_is_byte_identical_when_cache_budget_forces_uncached_reads(
    api_client, shared_source_page, monkeypatch
):
    engine = _postgres_engine()
    headers, marker, _ = shared_source_page
    monkeypatch.setattr(metadata_module, "get_utc_now", lambda: FIXED_TIME)
    with _candidate_queries(engine) as cached_scans:
        cached = api_client.get("/api/healthlabs/v1/recipes", headers=headers, params={"q": marker})
    with monkeypatch.context() as patch:
        patch.setattr(metadata_module, "_PROJECTION_CACHE_MAX_BYTES", 0)
        with _candidate_queries(engine) as uncached_scans:
            uncached = api_client.get("/api/healthlabs/v1/recipes", headers=headers, params={"q": marker})
    assert cached.status_code == uncached.status_code == 200
    assert cached.content == uncached.content
    assert len(cached_scans) == 1
    assert len(uncached_scans) == 20


@pytest.mark.parametrize("connection_bound", [False, True])
def test_uncommitted_source_updates_and_external_transactions_keep_read_your_writes(
    api_client, shared_source_page, monkeypatch, connection_bound
):
    from mealie.db.models.users import User
    from mealie.lang.providers import get_locale_provider
    from mealie.schema.user import PrivateUser
    from mealie.services.healthlabs.recipe_api_service import RecipeApiService

    engine = _postgres_engine()
    _, marker, source_id = shared_source_page
    monkeypatch.setattr(metadata_module, "get_utc_now", lambda: FIXED_TIME)
    with engine.connect() as connection:
        outer = connection.begin() if connection_bound else None
        request_session = Session(
            bind=connection if connection_bound else engine,
            autoflush=False,
            join_transaction_mode="create_savepoint",
        )
        try:
            owner_id = request_session.scalar(
                sa.select(HealthLabsFoodMatch.reviewed_by_user_id).where(
                    HealthLabsFoodMatch.nutrient_food_id == source_id
                )
            )
            user = PrivateUser.model_validate(request_session.get(User, owner_id))
            service = RecipeApiService(request_session, user, get_locale_provider("en-US"))
            source = request_session.get(HealthLabsNutrientFood, source_id)
            source.nutrient_values = {**source.nutrient_values, "protein_g": {"amount": 20, "unit": "g"}}
            request_session.flush()
            assert not request_session.new and not request_session.dirty
            page = service.list_recipes(q=marker).model_dump(mode="json", by_alias=True)
            for recipe in page["items"]:
                quantity = recipe["ingredients"][0]["quantity"]
                assert recipe["nutritionMetadata"]["perServing"]["proteinG"]["verified"] == pytest.approx(quantity / 5)
            assert request_session.in_transaction()
            assert request_session.scalar(sa.text("SHOW transaction_read_only")) == "off"
            if outer is not None:
                assert outer.is_active and connection.in_transaction()
        finally:
            request_session.rollback()
            request_session.close()
            if outer is not None:
                assert outer.is_active
                outer.rollback()
    with Session(engine) as verify:
        assert verify.get(HealthLabsNutrientFood, source_id).nutrient_values["protein_g"]["amount"] == 10


def test_snapshot_does_not_need_another_auth_pool_connection(api_client, shared_source_page, monkeypatch):
    from mealie.app import app

    engine = _postgres_engine()
    headers, marker, _ = shared_source_page
    isolated_auth_engine = sa.create_engine(engine.url, pool_size=1, max_overflow=0, pool_timeout=0.1)
    previous_override = app.dependency_overrides[generate_session]
    original_inputs = metadata_module.RecipeMetadataService.inputs
    source_connections = []

    def inspect_source(service, recipe):
        connection = service.session.get_bind()
        assert isinstance(connection, sa.engine.Connection)
        assert isinstance(connection.engine.pool, sa.pool.NullPool)
        assert isolated_auth_engine.pool.checkedout() == 1
        assert service.session.scalar(sa.text("SHOW transaction_isolation")) == "repeatable read"
        assert service.session.scalar(sa.text("SHOW transaction_read_only")) == "on"
        source_connections.append(connection)
        return original_inputs(service, recipe)

    def original_session():
        with Session(isolated_auth_engine) as session:
            yield session

    try:
        app.dependency_overrides[generate_session] = original_session
        monkeypatch.setattr(metadata_module.RecipeMetadataService, "inputs", inspect_source)
        response = api_client.get("/api/healthlabs/v1/recipes", headers=headers, params={"q": marker})
        assert response.status_code == 200, response.text
        assert len(source_connections) == 20
        assert all(connection.closed for connection in source_connections)
        assert isolated_auth_engine.pool.checkedout() == 0
    finally:
        app.dependency_overrides[generate_session] = previous_override
        isolated_auth_engine.dispose()


def test_projection_failure_rolls_back_and_closes_only_its_source_connection(
    api_client, shared_source_page, monkeypatch
):
    from mealie.db.models.healthlabs import HealthLabsAccessAudit

    engine = _postgres_engine()
    headers, marker, _ = shared_source_page
    source_connections = []

    def fail(service, recipe):
        connection = service.session.get_bind()
        assert isinstance(connection, sa.engine.Connection)
        source_connections.append(connection)
        raise ValueError("test projection failure")

    with Session(engine) as baseline:
        audits_before = baseline.scalar(sa.select(sa.func.count()).select_from(HealthLabsAccessAudit))
    monkeypatch.setattr(metadata_module.RecipeMetadataService, "project", fail)
    response = api_client.get("/api/healthlabs/v1/recipes", headers=headers, params={"q": marker})
    assert response.status_code == 400
    assert source_connections and all(connection.closed for connection in source_connections)
    with Session(engine) as verify:
        assert verify.scalar(sa.select(sa.func.count()).select_from(HealthLabsAccessAudit)) == audits_before
        assert verify.scalar(sa.text("SELECT 1")) == 1


@contextmanager
def _read_sources():
    from mealie.services.healthlabs.nutrition_service import NutritionService

    engine = _postgres_engine()
    read_engine = sa.create_engine(engine.url, poolclass=sa.pool.NullPool)
    try:
        with read_engine.connect().execution_options(
            isolation_level="REPEATABLE READ", postgresql_readonly=True
        ) as connection:
            with Session(bind=connection, autoflush=False) as session:
                session.begin()
                nutrition = NutritionService(session)
                cache = metadata_module._ProjectionLookupCache(session, on_invalidate=nutrition._clear_cache)
                try:
                    yield session, cache
                finally:
                    cache.close()
    finally:
        read_engine.dispose()


def test_snapshot_cache_is_bounded_immutable_and_returns_independent_source_material(
    unique_user_fn_scoped, monkeypatch
):
    from dataclasses import asdict

    from mealie.db.models.healthlabs import HealthLabsFoodPortion
    from tests.integration_tests.healthlabs.test_recipe_metadata_reads import _recipe

    user = unique_user_fn_scoped
    recipe, source = _recipe(user)
    user.repos.session.add(
        HealthLabsFoodPortion(
            nutrient_food_id=source.id, description="cup", amount=1, unit="cup", grams=200, source_managed=True
        )
    )
    user.repos.session.commit()
    with _read_sources() as (session, cache):
        service = metadata_module.RecipeMetadataService(session, lookups=cache)
        uncached = metadata_module.RecipeMetadataService(session)
        before = uncached.inputs(recipe)
        first = service.inputs(recipe)
        assert asdict(first) == asdict(before)
        assert first.ingredients[0].candidates and first.ingredients[0].portions
        first.ingredients[0].candidates[0].nutrients_per_100g["protein_g"] = 999
        first.ingredients[0].portions[0].grams = 999
        again = service.inputs(recipe)
        assert asdict(again) == asdict(before)
        assert all(isinstance(key, bytes) and isinstance(value, bytes) for key, value in cache.values.items())
        assert cache.byte_size == sum(len(key) + len(value) for key, value in cache.values.items())
        assert cache.byte_size <= metadata_module._PROJECTION_CACHE_MAX_BYTES
        assert len(cache.values) <= metadata_module._PROJECTION_CACHE_MAX_ENTRIES
        with monkeypatch.context() as patch:
            patch.setattr(metadata_module, "_PROJECTION_CACHE_MAX_BYTES", cache.byte_size + 1024)
            size = cache.byte_size
            assert cache.remember(("oversized", "key" * 1000), lambda: []) == []
            assert cache.byte_size == size
            patch.setattr(metadata_module, "_PROJECTION_CACHE_MAX_ENTRIES", len(cache.values))
            assert cache.remember(("entry-cap",), lambda: [1, 2, 3]) == [1, 2, 3]
            assert len(cache.values) <= metadata_module._PROJECTION_CACHE_MAX_ENTRIES


@pytest.mark.parametrize("finish", ["commit", "rollback"])
def test_commit_or_rollback_expires_every_snapshot_source_cache(unique_user_fn_scoped, finish):
    from tests.integration_tests.healthlabs.test_recipe_metadata_reads import _recipe

    user = unique_user_fn_scoped
    recipe, source = _recipe(user)
    with _read_sources() as (session, cache):
        service = metadata_module.RecipeMetadataService(session, lookups=cache)
        before = service.inputs(recipe)
        assert cache.values
        getattr(session, finish)()
        assert not cache.enabled and cache.byte_size == 0 and cache.values == {}
        with Session(_postgres_engine()) as writer:
            food = writer.get(HealthLabsNutrientFood, source.id)
            food.nutrient_values = {**food.nutrient_values, "protein_g": {"amount": 20, "unit": "g"}}
            writer.commit()
        after = service.inputs(recipe)
        assert before.authoring_fingerprint == after.authoring_fingerprint
        assert before.input_fingerprint != after.input_fingerprint
        assert after.ingredients[0].nutrients_per_100g["protein_g"] == 20
        assert cache.values == {}


def test_candidate_country_group_preparation_and_portion_scopes_are_kept_separate(
    unique_user_fn_scoped,
):
    _postgres_engine()
    from mealie.db.models.group import Group
    from mealie.db.models.healthlabs import HealthLabsFoodPortion
    from tests.unit_tests.healthlabs.test_nutrition_calculation import _nutrient_food, _source

    user = unique_user_fn_scoped
    name = f"scoped rice {uuid4().hex}"
    gb = _nutrient_food(user, _source(user, country="GB"), name=name, state="raw")
    cooked = _nutrient_food(user, _source(user, country="GB"), name=name, state="cooked")
    fr = _nutrient_food(user, _source(user, country="FR"), name=name, state="raw")
    foreign_group = Group(name=f"Foreign {uuid4()}", slug=f"foreign-{uuid4().hex}", session=user.repos.session)
    user.repos.session.add(foreign_group)
    user.repos.session.flush()
    own = HealthLabsFoodPortion(
        nutrient_food_id=gb.id,
        group_id=user.group_id,
        source_managed=False,
        description="own cup",
        amount=1,
        unit="cup",
        grams=210,
    )
    foreign = HealthLabsFoodPortion(
        nutrient_food_id=gb.id,
        group_id=foreign_group.id,
        source_managed=False,
        description="foreign cup",
        amount=1,
        unit="cup",
        grams=310,
    )
    public = HealthLabsFoodPortion(
        nutrient_food_id=gb.id,
        source_managed=True,
        description="public cup",
        amount=1,
        unit="cup",
        grams=200,
    )
    user.repos.session.add_all([own, foreign, public])
    user.repos.session.commit()
    with _read_sources() as (session, cache):
        british = metadata_module.RecipeMetadataService(session, country="GB", lookups=cache)
        french = metadata_module.RecipeMetadataService(session, country="FR", lookups=cache)
        assert [row.id for row in british._candidates(name, group_id=user.group_id, preparation_state="raw")] == [gb.id]
        assert [row.id for row in french._candidates(name, group_id=user.group_id, preparation_state="raw")] == [fr.id]
        assert [row.id for row in british._candidates(name, group_id=user.group_id, preparation_state="cooked")] == [
            cooked.id
        ]
        assert {row.id for row in british._portions(gb.id, user.group_id)} == {own.id, public.id}
        assert {row.id for row in british._portions(gb.id, foreign_group.id)} == {foreign.id, public.id}
        assert {row.id for row in british._portions(gb.id, None)} == {public.id}


def test_cached_native_food_and_unit_facts_do_not_cross_recipe_groups(unique_user_fn_scoped):
    from tests.integration_tests.healthlabs.test_recipe_metadata_reads import _recipe

    user = unique_user_fn_scoped
    recipe, _ = _recipe(user)
    foreign = recipe.model_copy(deep=True, update={"group_id": uuid4()})
    with _read_sources() as (session, cache):
        service = metadata_module.RecipeMetadataService(session, lookups=cache)
        accepted = service.inputs(recipe)
        assert accepted.ingredients[0].accepted_source_food_id is not None
        denied = service.inputs(foreign)
        assert denied.ingredients[0].accepted_source_food_id is None
        assert denied.ingredients[0].candidates == []


@pytest.fixture
def cross_subject_page(api_client, healthlabs_provision, healthlabs_recipe_food, healthlabs_set_capabilities):
    from mealie.schema.healthlabs.identity import HealthLabsCapability

    first, first_headers = healthlabs_provision()
    second, second_headers = healthlabs_provision()
    marker = f"audit{uuid4().hex}"
    healthlabs_recipe_food(first_headers, f"own rice {marker}")
    healthlabs_recipe_food(second_headers, f"other rice {marker}")
    healthlabs_set_capabilities(
        first["userId"], HealthLabsCapability.RECIPES_READ_OWN, HealthLabsCapability.RECIPES_READ_ALL
    )
    return first, first_headers, second, marker


def test_cross_subject_read_audit_is_committed_on_original_writable_session(
    api_client, cross_subject_page, monkeypatch
):
    from mealie.db.models.healthlabs import HealthLabsAccessAudit

    engine = _postgres_engine()
    first, headers, second, marker = cross_subject_page
    original = metadata_module.RecipeMetadataService.inputs
    source_readonly = []

    def inspect_source(service, recipe):
        source_readonly.append(service.session.scalar(sa.text("SHOW transaction_read_only")))
        assert not any(isinstance(row, HealthLabsAccessAudit) for row in service.session.new)
        return original(service, recipe)

    monkeypatch.setattr(metadata_module.RecipeMetadataService, "inputs", inspect_source)
    response = api_client.get(
        "/api/healthlabs/v1/recipes",
        headers={**headers, "X-HealthLabs-Access-Reason": "Projection audit test"},
        params={"q": marker},
    )
    assert response.status_code == 200, response.text
    assert len(response.json()["items"]) == 2
    assert source_readonly == ["on", "on"]
    with Session(engine) as verify:
        audits = verify.scalars(
            sa.select(HealthLabsAccessAudit).where(
                HealthLabsAccessAudit.actor_user_id == UUID(first["userId"]),
                HealthLabsAccessAudit.action == "recipes.read_cross_subject",
            )
        ).all()
        assert len(audits) == 1
        assert audits[0].reason == "Projection audit test"


def test_cross_subject_projection_failure_does_not_commit_audit_or_leak_connection(
    api_client, cross_subject_page, monkeypatch
):
    from mealie.db.models.healthlabs import HealthLabsAccessAudit

    engine = _postgres_engine()
    first, headers, second, marker = cross_subject_page
    original = metadata_module.RecipeMetadataService.project
    source_connections = []

    def fail_foreign(service, recipe):
        source_connections.append(service.session.get_bind())
        if recipe.user_id == UUID(second["userId"]):
            raise ValueError("test foreign projection failure")
        return original(service, recipe)

    monkeypatch.setattr(metadata_module.RecipeMetadataService, "project", fail_foreign)
    response = api_client.get(
        "/api/healthlabs/v1/recipes",
        headers={**headers, "X-HealthLabs-Access-Reason": "Projection audit test"},
        params={"q": marker},
    )
    assert response.status_code == 400
    assert source_connections and all(connection.closed for connection in source_connections)
    with Session(engine) as verify:
        assert (
            verify.scalar(
                sa.select(sa.func.count())
                .select_from(HealthLabsAccessAudit)
                .where(
                    HealthLabsAccessAudit.actor_user_id == UUID(first["userId"]),
                    HealthLabsAccessAudit.action == "recipes.read_cross_subject",
                )
            )
            == 0
        )


def test_snapshot_connection_rejects_source_writes(unique_user_fn_scoped):
    from tests.integration_tests.healthlabs.test_recipe_metadata_reads import _recipe

    recipe, source = _recipe(unique_user_fn_scoped)
    with _read_sources() as (session, cache):
        assert metadata_module.RecipeMetadataService(session, lookups=cache).inputs(recipe).ingredients
        with pytest.raises(sa.exc.DBAPIError):
            session.execute(
                sa.update(HealthLabsNutrientFood).where(HealthLabsNutrientFood.id == source.id).values(name="forbidden")
            )
    with Session(_postgres_engine()) as verify:
        assert verify.get(HealthLabsNutrientFood, source.id).name != "forbidden"
