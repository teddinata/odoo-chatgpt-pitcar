{
    'name': 'JARVIS AI PITCAR',
    'category': 'Productivity',
    'summary': 'Integration with OpenAI API',
    'icon': '/pitcar_chatgpt/static/pitcar-modified.png',
    'description': """
        This module integrates Odoo with OpenAI's GPT models for:
        - Text generation
        - Content analysis
        - Data insights
    """,
    'sequence': 2,
    'author': 'Teddinata',
    'website': 'https://www.pitcar.co.id',
    'depends': ['base_setup'],
    'data': [
        'security/ir.model.access.csv',
        'views/res_config_settings_views.xml',
        'views/openai_views.xml',
    ],
    'installable': True,
    'application': True,
    'auto_install': False,
    'default_timezone': 'Asia/Jakarta',
    'license': 'LGPL-3',
    'version':'16.0.1',
    'external_dependencies': {
        'python': ['openai'],
    },
}