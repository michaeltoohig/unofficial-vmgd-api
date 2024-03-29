"""WeatherWarning

Revision ID: aedf4cbbc1ef
Revises: b12db961fd0c
Create Date: 2023-05-05 15:41:39.593988

"""
from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision = 'aedf4cbbc1ef'
down_revision = 'b12db961fd0c'
branch_labels = None
depends_on = None


def upgrade() -> None:
    # ### commands auto generated by Alembic - please adjust! ###
    op.create_table('warning',
    sa.Column('id', sa.Integer(), nullable=False),
    sa.Column('session_id', sa.Integer(), nullable=False),
    sa.Column('issued_at', sa.DateTime(timezone=True), nullable=False),
    sa.Column('date', sa.DateTime(timezone=True), nullable=False),
    sa.Column('no_current_warning', sa.Boolean(), nullable=False),
    sa.Column('body', sa.String(), nullable=True),
    sa.ForeignKeyConstraint(['session_id'], ['session.id'], ),
    sa.PrimaryKeyConstraint('id')
    )
    with op.batch_alter_table('warning', schema=None) as batch_op:
        batch_op.create_index(batch_op.f('ix_warning_id'), ['id'], unique=False)

    # ### end Alembic commands ###


def downgrade() -> None:
    # ### commands auto generated by Alembic - please adjust! ###
    with op.batch_alter_table('warning', schema=None) as batch_op:
        batch_op.drop_index(batch_op.f('ix_warning_id'))

    op.drop_table('warning')
    # ### end Alembic commands ###
