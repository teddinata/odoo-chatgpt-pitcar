from odoo import api, fields, models

class ResConfigSettings(models.TransientModel):
    _inherit = 'res.config.settings'

    openai_api_key = fields.Char(string='OpenAI API Key', config_parameter='openai.api_key')
    openai_model = fields.Selection([
        ('gpt-3.5-turbo', 'GPT-3.5 Turbo'),
        ('gpt-4', 'GPT-4'),
    ], string='Default Model', config_parameter='openai.model', default='gpt-3.5-turbo')