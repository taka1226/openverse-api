import datetime
import logging as log
import os

import psycopg2
from psycopg2.extras import DictCursor
from psycopg2.sql import SQL, Identifier, Literal

from ingestion_server.cleanup import clean_image_data
from ingestion_server.indexer import database_connect
from ingestion_server.queries import (
    get_fdw_query,
    get_copy_data_query,
    get_go_live_query,
)

"""
Pull the latest copy of a table from the upstream database (aka CC Catalog/the
intermediary database).

Since some of these tables have hundreds of millions of records and are tens of
gigabytes in size, there are some performance considerations we need to account
for. Appending or updating large numbers of records has poor performance due to
the number of indices and constraints on the table. These indices and
constraints are necessary for good query performance and data consistency,
so we can't get rid of them. Since the data is being actively queried in
production, disabling indices and constraints temporarily isn't an option.

To work around these problems, we need to create a temporary table, import the
data, and only then create indices and constraints. Then, "promote" the new
table to replace the old data. This strategy is far faster than updating the
data in place.
"""

UPSTREAM_DB_HOST = os.environ.get('UPSTREAM_DB_HOST', 'upstream_db')
UPSTREAM_DB_USER = os.environ.get('UPSTREAM_DB_USER', 'deploy')
UPSTREAM_DB_PASSWORD = os.environ.get('UPSTREAM_DB_PASSWORD', 'deploy')
UPSTREAM_DATABASE_NAME = os.environ.get('UPSTREAM_DATABASE_NAME', 'openledger')
UPSTREAM_DB_PORT = int(os.environ.get('UPSTREAM_DB_PORT', 5432))

RELATIVE_UPSTREAM_DB_HOST = os.environ.get(
    'RELATIVE_UPSTREAM_DB_HOST',
    UPSTREAM_DB_HOST
)
"""The hostname of the upstream DB from the POV of the downstream DB"""
RELATIVE_UPSTREAM_DB_PORT = int(os.environ.get(
    'RELATIVE_UPSTREAM_DB_PORT',
    UPSTREAM_DB_PORT
))
"""The port of the upstream DB from the POV of the downstream DB"""


def _get_shared_cols(downstream, upstream, table: str):
    """
    Given two database connections and a table name, return the list of columns
    that the two tables have in common. The upstream table has the "_view"
    suffix attached to it.

    :param downstream: an open connection to the downstream PostgreSQL database
    :param upstream: an open connection to the upstream PostgreSQL database
    :param table: the name of the downstream table
    :return: a list of the column names that are common to both databases
    """
    with downstream.cursor() as cur1, upstream.cursor() as cur2:
        get_tables = SQL('SELECT * FROM {table} LIMIT 0;')
        cur1.execute(get_tables.format(table=Identifier(table)))
        conn1_cols = set([desc[0] for desc in cur1.description])
        cur2.execute(get_tables.format(table=Identifier(f'{table}_view')))
        conn2_cols = set([desc[0] for desc in cur2.description])

    shared = conn1_cols.intersection(conn2_cols)
    shared.add('standardized_popularity')
    log.info(f'Shared columns: {shared}')
    return list(shared)


def _update_progress(progress, new_value):
    if progress:
        progress.value = new_value


def _generate_indices(conn, table: str):
    """
    Using the existing table as a template, generate CREATE INDEX statements for
    the new table.

    :param conn: A connection to the API database.
    :param table: The table to be updated.
    :return: A list of CREATE INDEX statements.
    """
    def _clean_idxs(indices):
        # Remove names of indices. We don't want to collide with the old names;
        # we want the database to generate them for us upon recreating the
        # table.
        cleaned = []
        for index in indices:
            # The index name is always after CREATE [UNIQUE] INDEX; delete it.
            tokens = index[0].split(' ')
            index_idx = tokens.index('INDEX')
            del tokens[index_idx + 1]
            # The table name is always after ON. Rename it to match the
            # temporary copy of the data.
            on_idx = tokens.index('ON')
            table_name_idx = on_idx + 1
            schema_name, table_name = tokens[table_name_idx].split('.')
            tokens[table_name_idx] = f'{schema_name}.temp_import_{table_name}'
            if 'id' not in index:
                cleaned.append(' '.join(tokens))

        return cleaned

    # Get all of the old indices from the existing table.
    with conn.cursor() as cur:
        get_idxs = SQL(
            'SELECT indexdef '
            'FROM pg_indexes '
            'WHERE tablename = {table};'
        ).format(table=Literal(table))
        cur.execute(get_idxs)
        idxs = cur.fetchall()
    cleaned_idxs = _clean_idxs(idxs)
    return cleaned_idxs


def _generate_constraints(conn, table: str):
    """
    Using the existing table as a template, generate ALTER TABLE ADD CONSTRAINT
    statements pointing to the new table.

    :return: A list of SQL statements.
    """
    # List all active constraints across the database.
    get_all_constraints = SQL('''
        SELECT conrelid::regclass AS table, conname, pg_get_constraintdef(c.oid)
        FROM pg_constraint AS c
        JOIN pg_namespace AS n
        ON n.oid = c.connamespace
        AND n.nspname = 'public'
        ORDER BY conrelid::regclass::text, contype DESC;
    ''')
    with conn.cursor(cursor_factory=DictCursor) as cur:
        cur.execute(get_all_constraints)
        all_constraints = cur.fetchall()
    # Find all constraints that either exist inside of the table or
    # reference it from another table. Ignore PRIMARY KEY statements.
    remap_constraints = []
    drop_orphans = []
    for constraint in all_constraints:
        statement = constraint['pg_get_constraintdef']
        con_table = constraint['table']
        is_fk = _is_foreign_key(statement, table)
        if (con_table == table or is_fk) and 'PRIMARY KEY' not in statement:
            alter_stmnts = _remap_constraint(
                constraint['conname'], con_table, statement, table
            )
            remap_constraints.extend(alter_stmnts)
            if is_fk:
                del_orphans = _generate_delete_orphans(statement, con_table)
                drop_orphans.append(del_orphans)

    constraint_statements = []
    constraint_statements.extend(drop_orphans)
    constraint_statements.extend(remap_constraints)
    return constraint_statements


def _is_foreign_key(_statement, table):
    return f'REFERENCES {table}(' in _statement


def _generate_delete_orphans(fk_statement, fk_table):
    """
    Sometimes, upstream data is deleted. If there are foreign key
    references to deleted data, we must delete them before adding
    constraints back to the table. To accomplish this, parse the
    foreign key statement and generate the deletion statement.
    """
    fk_tokens = fk_statement.split(' ')
    fk_field_idx = fk_tokens.index('KEY') + 1
    fk_ref_idx = fk_tokens.index('REFERENCES') + 1
    fk_field = fk_tokens[fk_field_idx].replace('(', '').replace(')', '')
    fk_reference = fk_tokens[fk_ref_idx]
    ref_table, ref_field = fk_reference.split('(')
    ref_field = ref_field.replace(')', '')

    del_orphans = SQL(
        'DELETE FROM {fk_table} AS fk_table '
        'WHERE NOT EXISTS(SELECT 1 FROM {ref_table} AS r '
        'WHERE {ref_field} = {fk_field});'
    ).format(
        fk_table=Identifier(fk_table),
        ref_table=Identifier(f'temp_import_{ref_table}'),
        ref_field=Identifier('r', ref_field),
        fk_field=Identifier('fk_table', fk_field),
    )
    return del_orphans


def _remap_constraint(name, con_table, fk_statement, table):
    """ Produce ALTER TABLE ... statements for each constraint."""
    alterations = [
        SQL('ALTER TABLE {con_table} DROP CONSTRAINT {name}').format(
            con_table=Identifier(con_table),
            name=Identifier(name)
        )
    ]
    # Constraint applies to the table we're replacing
    if con_table == table:
        alterations.append(
            SQL('ALTER TABLE {con_table} ADD {fk_statement}').format(
                con_table=Identifier(con_table),
                fk_statement=SQL(fk_statement),
            )
        )
    # Constraint references the table we're replacing. Point it at the new
    # one.
    else:
        tokens = fk_statement.split(' ')
        # Point the constraint to the new table.
        reference_idx = tokens.index('REFERENCES') + 1
        table_reference = tokens[reference_idx]
        match_old_ref = f'{table}('
        new_ref = f'temp_import_{table}('
        new_reference = table_reference.replace(match_old_ref, new_ref)
        tokens[reference_idx] = new_reference
        con_definition = ' '.join(tokens)
        create_constraint = SQL(
            'ALTER TABLE {con_table} ADD {con_definition}'
        ).format(
            con_table=Identifier(con_table),
            con_definition=SQL(con_definition)
        )
        alterations.append(create_constraint)
    return alterations


def reload_upstream(table, progress=None, finish_time=None):
    """
    Import updates from the upstream CC Catalog database into the API. The
    process involves the following steps.

    1. Get the list of overlapping columns: ``_get_shared_cols``
    2. Create FDW for the data transfer: ``get_fdw_query``
    3. Import data into a temporary table: ``get_copy_data_query``
    4. Clean the data: ``clean_image_data``
    5. Recreate indices from the original table: ``_generate_indices``
    6. Recreate constraints from the original table: ``_generate_constraints``
    7. Promote the temp table and delete the original: ``get_go_live_query``

    This is the main function of this module.

    :param table: The upstream table to copy.
    :param progress: multiprocessing.Value float for sharing task progress
    :param finish_time: multiprocessing.Value int for sharing finish timestamp
    """

    # Step 1: Get the list of overlapping columns
    downstream_db = database_connect()
    upstream_db = psycopg2.connect(
        dbname=UPSTREAM_DATABASE_NAME,
        user=UPSTREAM_DB_USER,
        port=UPSTREAM_DB_PORT,
        password=UPSTREAM_DB_PASSWORD,
        host=UPSTREAM_DB_HOST,
        connect_timeout=5
    )
    shared_cols = _get_shared_cols(downstream_db, upstream_db, table)
    upstream_db.close()

    with downstream_db.cursor() as downstream_cur:
        # Step 2: Create FDW for the data transfer
        log.info('(Re)initializing foreign data wrapper')
        init_fdw = get_fdw_query(
            RELATIVE_UPSTREAM_DB_HOST,
            RELATIVE_UPSTREAM_DB_PORT,
            UPSTREAM_DATABASE_NAME,
            UPSTREAM_DB_USER,
            UPSTREAM_DB_PASSWORD,
            f'{table}_view',
        )
        downstream_cur.execute(init_fdw)

        # Step 3: Import data into a temporary table
        log.info('Copying upstream data...')
        copy_data = get_copy_data_query(table, shared_cols)
        downstream_cur.execute(copy_data)
    downstream_db.commit()
    downstream_db.close()

    if table != 'audio':
        # Step 4: Clean the data
        log.info('Cleaning data...')
        clean_image_data(table)

    downstream_db = database_connect()
    with downstream_db.cursor() as downstream_cur:
        # Step 5: Recreate indices from the original table
        log.info('Copying finished! Recreating database indices...')
        create_indices = ';\n'.join(_generate_indices(downstream_db, table))
        _update_progress(progress, 50.0)
        if create_indices != '':
            downstream_cur.execute(create_indices)
        _update_progress(progress, 70.0)

        # Step 6: Recreate constraints from the original table
        log.info('Done creating indices! Remapping constraints...')
        remap_constraints = SQL(';\n').join(
            _generate_constraints(downstream_db, table)
        )
        if remap_constraints != '':
            downstream_cur.execute(remap_constraints)
        _update_progress(progress, 99.0)

        # Step 7: Promote the temporary table and delete the original
        log.info('Done remapping constraints! Going live with new table...')
        go_live = get_go_live_query(table)
        downstream_cur.execute(go_live)
    downstream_db.commit()
    downstream_db.close()
    log.info(f"Finished refreshing table '{table}'.")
    _update_progress(progress, 100.0)

    if finish_time:
        finish_time.value = datetime.datetime.utcnow().timestamp()
