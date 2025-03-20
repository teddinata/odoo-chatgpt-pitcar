from odoo import models, fields, api
from openai import OpenAI
from odoo.exceptions import UserError
import logging
from datetime import datetime, timedelta

_logger = logging.getLogger(__name__)

class OpenAIPrompt(models.Model):
    _name = 'openai.prompt'
    _description = 'OpenAI Prompt'
    _order = 'create_date desc'

    name = fields.Char('Name', required=True)
    prompt = fields.Text('Prompt', required=True)
    response = fields.Text('Response', readonly=True)
    model = fields.Selection([
        ('gpt-3.5-turbo', 'GPT-3.5 Turbo'),
        ('gpt-4', 'GPT-4'),
        ('gpt-4-turbo', 'GPT-4-turbo'),
        ('gpt-4o-mini', 'GPT-4o Mini'),
        ('gpt-4o-2024-11-20', 'GPT-4o'),
    ], string='Model', required=True)
    state = fields.Selection([
        ('draft', 'Draft'),
        ('done', 'Completed'),
        ('error', 'Error'),
    ], string='Status', default='draft', readonly=True)
    error_message = fields.Text('Error Message', readonly=True)
    token_count = fields.Integer('Token Count', readonly=True)
    
    def get_sales_data(self):
        """Helper function untuk mengambil data penjualan"""
        today = datetime.today()
        current_month_start = today.replace(day=1)
        last_month_end = current_month_start - timedelta(days=1)
        last_month_start = last_month_end.replace(day=1)

        # Query data penjualan
        current_month_sales = self.env['sale.order'].search([
            ('date_order', '>=', current_month_start),
            ('date_order', '<=', today),
            ('state', 'in', ['sale', 'done'])
        ])
        last_month_sales = self.env['sale.order'].search([
            ('date_order', '>=', last_month_start),
            ('date_order', '<=', last_month_end),
            ('state', 'in', ['sale', 'done'])
        ])

        sales_info = f"""
Current Month Sales ({current_month_start.strftime('%B %Y')}):
- Total Orders: {len(current_month_sales)}
- Total Amount: {sum(current_month_sales.mapped('amount_total')):,.2f}

Last Month Sales ({last_month_start.strftime('%B %Y')}):
- Total Orders: {len(last_month_sales)}
- Total Amount: {sum(last_month_sales.mapped('amount_total')):,.2f}
"""
        return sales_info

    @api.model
    def default_get(self, fields_list):
        res = super().default_get(fields_list)
        res['model'] = self.env['ir.config_parameter'].sudo().get_param('openai.model', 'gpt-3.5-turbo')
        return res

    def action_generate_response(self):
        self.ensure_one()
        api_key = self.env['ir.config_parameter'].sudo().get_param('openai.api_key')
        
        if not api_key:
            raise UserError('Please configure OpenAI API Key in settings first!')

        try:
            # Inisialisasi client OpenAI dengan API key
            client = OpenAI(api_key=api_key)
            
            # Tambahkan data penjualan jika prompt mengandung kata kunci tertentu
            prompt_lower = self.prompt.lower()
            if any(keyword in prompt_lower for keyword in ['sales', 'revenue', 'penjualan', 'pendapatan']):
                enhanced_prompt = f"""
{self.prompt}

Here's the relevant sales data:
{self.get_sales_data()}

Please analyze this data and provide insights.
"""
            else:
                enhanced_prompt = self.prompt
            
            # Buat request ke API dengan sintaks baru
            response = client.chat.completions.create(
                model=self.model,
                messages=[
                    {"role": "user", "content": enhanced_prompt}
                ]
            )
            
            # Update record dengan response
            self.write({
                'response': response.choices[0].message.content,
                'state': 'done',
                'token_count': response.usage.total_tokens,
                'error_message': False
            })
            
        except Exception as e:
            _logger.error('OpenAI API Error: %s', str(e))
            self.write({
                'state': 'error',
                'error_message': str(e)
            })