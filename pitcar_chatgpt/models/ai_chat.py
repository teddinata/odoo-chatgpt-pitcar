from odoo import models, fields, api, _, exceptions, tools
from odoo.exceptions import ValidationError, AccessError, UserError
from openai import OpenAI
import logging
import json
import pytz
from datetime import datetime, timedelta
import uuid
import re

_logger = logging.getLogger(__name__)

class AIUserSettings(models.Model):
    _name = 'ai.user.settings'
    _description = 'AI User Settings'
    _rec_name = 'user_id'
    
    user_id = fields.Many2one('res.users', string='User', required=True, index=True, ondelete='cascade')
    company_id = fields.Many2one('res.company', string='Company', required=True, default=lambda self: self.env.company)
    
    # Daily usage limits
    daily_gpt4_limit = fields.Integer('Daily GPT-4 Limit', default=5)
    gpt4_usage_count = fields.Integer('GPT-4 Usage Today', default=0)
    last_reset_date = fields.Date('Last Reset Date', default=fields.Date.today)
    fallback_to_gpt35 = fields.Boolean('Fallback to GPT-3.5', default=True, 
                                      help="If enabled, requests will fall back to GPT-3.5 when GPT-4 limit is reached")
    
    # Model preferences
    default_model = fields.Selection([
        ('gpt-3.5-turbo', 'GPT-3.5 Turbo'),
        ('gpt-4', 'GPT-4'),
        ('gpt-4-turbo', 'GPT-4-turbo'),
        ('gpt-4o-mini', 'GPT-4o Mini'),
        ('gpt-4o', 'GPT-4o'),
    ], string='Default Model', default='gpt-3.5-turbo')
    
    # Generation settings
    temperature = fields.Float('Temperature', default=0.7, 
                             help="Higher values produce more random outputs")
    max_tokens = fields.Integer('Max Tokens', default=2000,
                              help="Maximum length of the generated response")
    
    # System prompt customization
    custom_system_prompt = fields.Text('Custom System Prompt',
                                     help="Additional instructions for the AI")
    
    # Analytics
    total_tokens_used = fields.Integer('Total Tokens Used', default=0)
    token_usage_this_month = fields.Integer('Tokens This Month', compute='_compute_token_usage_this_month')
    
    _sql_constraints = [
        ('user_company_unique', 'UNIQUE(user_id, company_id)', 'A user can only have one AI settings per company')
    ]
    
    def _compute_token_usage_this_month(self):
        """Compute token usage for the current month"""
        for record in self:
            today = fields.Date.today()
            start_of_month = today.replace(day=1)
            
            # Get all messages from this user this month
            messages = self.env['ai.chat.message'].search([
                ('create_date', '>=', start_of_month),
                ('chat_id.user_id', '=', record.user_id.id),
                ('token_count', '>', 0)
            ])
            
            record.token_usage_this_month = sum(messages.mapped('token_count'))
    
    @api.model
    def _cron_reset_daily_limits(self):
        """Cron job to reset daily limits for all users"""
        today = fields.Date.today()
        settings_to_reset = self.search([
            ('last_reset_date', '<', today)
        ])
        settings_to_reset.write({
            'gpt4_usage_count': 0,
            'last_reset_date': today
        })
        _logger.info(f"Reset GPT-4 limits for {len(settings_to_reset)} users")
    
    @api.model
    def get_user_settings(self, user_id=None):
        """Get or create settings for the given user"""
        if not user_id:
            user_id = self.env.user.id
        
        settings = self.search([
            ('user_id', '=', user_id),
            ('company_id', '=', self.env.company.id)
        ], limit=1)
        
        if not settings:
            settings = self.create({
                'user_id': user_id,
                'company_id': self.env.company.id
            })
        
        # Check if we need to reset counter (if last_reset_date is not today)
        today = fields.Date.today()
        if settings.last_reset_date < today:
            settings.write({
                'gpt4_usage_count': 0,
                'last_reset_date': today
            })
        
        return settings
    
    def increment_gpt4_usage(self):
        """Increment GPT-4 usage counter and update last reset date if needed"""
        self.ensure_one()
        
        # Reset counter if needed
        today = fields.Date.today()
        if self.last_reset_date < today:
            self.gpt4_usage_count = 1
            self.last_reset_date = today
        else:
            self.gpt4_usage_count += 1
        
        return True
    
    def check_gpt4_limit(self):
        """Check if user has reached their GPT-4 limit"""
        self.ensure_one()
        
        # Reset counter if needed
        today = fields.Date.today()
        if self.last_reset_date < today:
            self.gpt4_usage_count = 0
            self.last_reset_date = today
            return True
        
        # Check if under limit
        return self.gpt4_usage_count < self.daily_gpt4_limit
    
class AIChatMessage(models.Model):
    _name = 'ai.chat.message'
    _description = 'AI Chat Message'
    _order = 'create_date asc'
    
    chat_id = fields.Many2one('ai.chat', string='Chat', required=True, ondelete='cascade')
    user_id = fields.Many2one('res.users', string='User', related='chat_id.user_id', store=True)
    message_type = fields.Selection([
        ('user', 'User'),
        ('assistant', 'Assistant'),
        ('system', 'System'),
    ], string='Message Type', required=True)
    content = fields.Text('Content', required=True)
    context_data = fields.Text('Context Data')
    model_used = fields.Char('Model Used')
    token_count = fields.Integer('Token Count')
    message_uuid = fields.Char('Message UUID', default=lambda self: str(uuid.uuid4()), readonly=True)
    response_time = fields.Float('Response Time (sec)')

class AIChat(models.Model):
    _name = 'ai.chat'
    _description = 'AI Chat Session'
    _order = 'last_message_date desc'
    _inherit = ['mail.thread', 'mail.activity.mixin']

    name = fields.Char('Chat Title', required=True, tracking=True)
    user_id = fields.Many2one('res.users', string='User', required=True, default=lambda self: self.env.user, 
                           readonly=True, tracking=True)
    message_ids = fields.One2many('ai.chat.message', 'chat_id', string='Messages')
    last_message_date = fields.Datetime('Last Message', compute='_compute_last_message', store=True)
    active = fields.Boolean(default=True, tracking=True)
    session_token = fields.Char(string='Session Token', default=lambda self: str(uuid.uuid4()), readonly=True)
    
    # For access control
    company_id = fields.Many2one('res.company', string='Company', required=True, 
                              default=lambda self: self.env.company)
    
    # Summary and context
    summary = fields.Text('Summary', compute='_compute_summary', store=True)
    topic = fields.Char('Topic', compute='_compute_topic', store=True)
    
    # Analytics
    total_tokens = fields.Integer('Total Tokens Used', compute='_compute_token_usage', store=True)
    total_messages = fields.Integer('Total Messages', compute='_compute_message_stats', store=True)
    avg_response_time = fields.Float('Avg Response Time (sec)', compute='_compute_response_time', store=True)
    
    # Status and system information
    state = fields.Selection([
        ('active', 'Active'),
        ('archived', 'Archived'),
    ], string='Status', default='active', tracking=True)
    
    # Usage tracking
    gpt4_count = fields.Integer('GPT-4 Usage Count', default=0)
    gpt35_count = fields.Integer('GPT-3.5 Usage Count', default=0)
    
    @api.depends('message_ids.create_date')
    def _compute_last_message(self):
        for chat in self:
            last_message = chat.message_ids.sorted('create_date', reverse=True)[:1]
            chat.last_message_date = last_message.create_date if last_message else chat.create_date
    
    @api.depends('message_ids.token_count')
    def _compute_token_usage(self):
        for chat in self:
            chat.total_tokens = sum(chat.message_ids.mapped('token_count') or [0])
    
    @api.depends('message_ids')
    def _compute_message_stats(self):
        for chat in self:
            chat.total_messages = len(chat.message_ids)
    
    @api.depends('message_ids.response_time')
    def _compute_response_time(self):
        for chat in self:
            response_times = chat.message_ids.filtered('response_time').mapped('response_time')
            chat.avg_response_time = sum(response_times) / len(response_times) if response_times else 0
    
    @api.depends('message_ids.content', 'message_ids.message_type')
    def _compute_summary(self):
        """Generate a summary of the conversation using the AI itself"""
        for chat in self:
            if len(chat.message_ids) >= 5:  # Only summarize if we have enough messages
                try:
                    # Get the last few messages to summarize
                    messages = chat.message_ids.sorted('create_date', reverse=True)[:10]
                    messages = messages.sorted('create_date')
                    
                    # Prepare context for summarization
                    content_to_summarize = "\n".join([
                        f"{'User' if msg.message_type == 'user' else 'AI'}: {msg.content}" 
                        for msg in messages
                    ])
                    
                    # Call OpenAI for summarization
                    api_key = self.env['ir.config_parameter'].sudo().get_param('openai.api_key')
                    if not api_key:
                        chat.summary = "API key not configured."
                        continue
                        
                    client = OpenAI(api_key=api_key)
                    response = client.chat.completions.create(
                        model="gpt-3.5-turbo",
                        messages=[
                            {"role": "system", "content": "Please provide a brief 1-2 sentence summary of this conversation."},
                            {"role": "user", "content": content_to_summarize}
                        ],
                        max_tokens=100
                    )
                    
                    chat.summary = response.choices[0].message.content.strip()
                except Exception as e:
                    _logger.error(f"Error generating summary: {str(e)}")
                    chat.summary = "Error generating summary."
            else:
                chat.summary = "Not enough messages to summarize."
    
    @api.depends('message_ids.content')
    def _compute_topic(self):
        """Extract the main topic from the first few messages"""
        for chat in self:
            if chat.message_ids:
                try:
                    # Get first few messages
                    messages = chat.message_ids.sorted('create_date')[:3]
                    
                    # Extract content
                    content = "\n".join([msg.content for msg in messages])
                    
                    # Call OpenAI to extract topic
                    api_key = self.env['ir.config_parameter'].sudo().get_param('openai.api_key')
                    if not api_key:
                        chat.topic = "New Chat"
                        continue
                        
                    client = OpenAI(api_key=api_key)
                    response = client.chat.completions.create(
                        model="gpt-3.5-turbo",
                        messages=[
                            {"role": "system", "content": "Please provide a very brief 2-4 word topic for this conversation."},
                            {"role": "user", "content": content}
                        ],
                        max_tokens=20
                    )
                    
                    chat.topic = response.choices[0].message.content.strip()
                except Exception as e:
                    _logger.error(f"Error extracting topic: {str(e)}")
                    chat.topic = "New Chat"
            else:
                chat.topic = "New Chat"
    
    @api.model
    def create(self, vals):
        """Override create to set default name based on date if not provided"""
        if not vals.get('name'):
            vals['name'] = f"Chat {datetime.now().strftime('%d/%m/%Y %H:%M')}"
        return super(AIChat, self).create(vals)
    
    def archive_chat(self):
        """Archive chat instead of deleting it"""
        self.ensure_one()
        self.write({
            'active': False,
            'state': 'archived'
        })
        return True
    
    def restore_chat(self):
        """Restore archived chat"""
        self.ensure_one()
        self.write({
            'active': True,
            'state': 'active'
        })
        return True
    
    def clear_messages(self):
        """Clear all messages in the chat but keep the chat record"""
        self.ensure_one()
        self.message_ids.unlink()
        return {
            'type': 'ir.actions.client',
            'tag': 'reload',
        }
    
    def send_message(self, content, model=None):
        """Send a message in this chat and get AI response"""
        self.ensure_one()
        
        if not content.strip():
            return {'error': 'Message cannot be empty'}
        
        # Check user access to this chat
        if self.user_id.id != self.env.user.id:
            return {'error': 'You do not have access to this chat'}
        
        # Get system settings
        api_key = self.env['ir.config_parameter'].sudo().get_param('openai.api_key')
        if not api_key:
            return {'error': 'OpenAI API key not configured'}
            
        # Get user settings
        user_settings = self.env['ai.user.settings'].get_user_settings()
        
        # Determine which model to use
        model_to_use = model or user_settings.default_model
        
        # If GPT-4 is requested, check daily limit
        if model_to_use.startswith('gpt-4'):
            if not user_settings.check_gpt4_limit():
                # If user has reached their limit, fallback or return error
                if user_settings.fallback_to_gpt35:
                    model_to_use = 'gpt-3.5-turbo'
                else:
                    return {
                        'error': f'You have reached your daily limit of {user_settings.daily_gpt4_limit} GPT-4 requests',
                        'remaining_tokens': 0
                    }
        
        # Create user message
        message_time = fields.Datetime.now()
        user_message = self.env['ai.chat.message'].create({
            'chat_id': self.id,
            'message_type': 'user',
            'content': content,
            'create_date': message_time,
        })
        
        try:
            # Analyze message to gather relevant data from Odoo
            context_data = self._gather_relevant_data(content)
            
            # Get system prompt (combining default and user customization)
            system_prompt = self._get_system_prompt(user_settings)
            
            # Get chat history for context (last 10 messages)
            chat_history = self.message_ids.sorted('create_date', reverse=True)[1:10]  # exclude the message we just created
            chat_history = chat_history.sorted('create_date')
            
            # Prepare messages for API
            messages = [{"role": "system", "content": system_prompt}]
            
            # Add chat history
            for msg in chat_history:
                role = "user" if msg.message_type == "user" else "assistant"
                messages.append({"role": role, "content": msg.content})
            
            # Add current message with context data if any
            current_msg_content = content
            if context_data:
                current_msg_content += f"\n\n[SYSTEM: Here is relevant data from the Odoo ERP system to help answer this question]\n{context_data}"
            
            messages.append({"role": "user", "content": current_msg_content})
            
            # Call OpenAI API
            client = OpenAI(api_key=api_key)
            response_time_start = datetime.now()
            
            response = client.chat.completions.create(
                model=model_to_use,
                messages=messages,
                temperature=user_settings.temperature or 0.7,
                max_tokens=user_settings.max_tokens or 2000
            )
            
            response_time = (datetime.now() - response_time_start).total_seconds()
            
            # Update message counts
            if model_to_use.startswith('gpt-4'):
                self.gpt4_count += 1
                user_settings.increment_gpt4_usage()
            else:
                self.gpt35_count += 1
            
            # Save AI response
            ai_response = self.env['ai.chat.message'].create({
                'chat_id': self.id,
                'message_type': 'assistant',
                'content': response.choices[0].message.content,
                'context_data': context_data if context_data else None,
                'model_used': model_to_use,
                'token_count': response.usage.total_tokens,
                'response_time': response_time,
                'create_date': fields.Datetime.now(),
            })
            
            # Return success with response data
            return {
                'success': True,
                'response': {
                    'content': ai_response.content,
                    'model_used': model_to_use,
                    'token_count': response.usage.total_tokens,
                    'id': ai_response.id,
                    'message_id': ai_response.message_uuid
                }
            }
            
        except Exception as e:
            _logger.error(f"OpenAI API Error: {str(e)}")
            
            # Create error message
            error_msg = f"Sorry, I encountered an error while processing your request: {str(e)}"
            self.env['ai.chat.message'].create({
                'chat_id': self.id,
                'message_type': 'system',
                'content': error_msg,
                'model_used': model_to_use,
                'create_date': fields.Datetime.now(),
            })
            
            return {'error': str(e)}
    
    def _get_system_prompt(self, user_settings):
        """Get system prompt combining default and user customizations"""
        base_prompt = """
You are an AI assistant integrated with Odoo ERP. 
You help users analyze business data and make informed decisions.
Below are some guidelines to follow:

1. When analyzing data, provide clear insights and actionable recommendations
2. Answer based only on the data provided, avoid making assumptions
3. For sales analysis, compare performance across time periods and calculate growth rates
4. For inventory analysis, identify low stock items and potential ordering needs
5. For finance analysis, highlight important metrics and trends
6. Use formatting to make your responses easy to read
7. If you don't have the data to answer a question, explain what data would be needed

The data in square brackets [DATA] is provided by the system - it's not visible to the user but provides you the context to answer their questions accurately.
"""
        
        # Add company information
        company_info = self._get_company_info()
        if company_info:
            base_prompt += f"\n\nCompany Information:\n{company_info}\n"
        
        # Add user customization if any
        if user_settings.custom_system_prompt:
            base_prompt += f"\n\nAdditional Guidelines:\n{user_settings.custom_system_prompt}"
        
        return base_prompt
    
    def _get_company_info(self):
        """Get basic company information"""
        company = self.company_id or self.env.company
        return f"""
- Name: {company.name}
- Website: {company.website or 'Not set'}
- Email: {company.email or 'Not set'}
- Phone: {company.phone or 'Not set'}
"""
    
    def _gather_relevant_data(self, message):
        """Analyze message and gather relevant data from Odoo"""
        # Analyze message to determine what data is needed
        data_categories = self._analyze_message(message)
        
        result = []
        
        # Get data for each identified category
        for category in data_categories:
            method_name = f"_get_{category}_data"
            if hasattr(self, method_name):
                try:
                    category_data = getattr(self, method_name)(message)
                    if category_data:
                        result.append(category_data)
                except Exception as e:
                    _logger.error(f"Error gathering {category} data: {str(e)}")
        
        # Join all data with separators
        if result:
            return "\n\n---\n\n".join(result)
        else:
            # Default basic company data if no specific data found
            return self._get_basic_company_data()
    
    def _analyze_message(self, message):
        """Analyze the message to determine what data to fetch"""
        message_lower = message.lower()
        
        # Define categories with keywords
        categories = {
            'sales': ['sales', 'revenue', 'customer', 'order', 'client', 'income', 'penjualan', 'pelanggan', 'pendapatan', 'pesanan'],
            'inventory': ['inventory', 'stock', 'product', 'warehouse', 'item', 'persediaan', 'stok', 'produk', 'gudang', 'barang'],
            'finance': ['invoice', 'payment', 'profit', 'loss', 'accounting', 'balance', 'faktur', 'pembayaran', 'keuntungan', 'kerugian', 'akuntansi', 'saldo'],
            'employees': ['employee', 'staff', 'hr', 'karyawan', 'pegawai', 'sdm'],
            'purchases': ['purchase', 'vendor', 'supplier', 'pembelian', 'vendor', 'pemasok'],
        }
        
        # Determine which categories match the message
        matched_categories = []
        for category, keywords in categories.items():
            if any(keyword in message_lower for keyword in keywords):
                matched_categories.append(category)
        
        # If no categories matched, return default
        if not matched_categories:
            return ['basic']
        
        return matched_categories
    
    def _get_basic_company_data(self):
        """Get basic company data as default"""
        company = self.company_id or self.env.company
        
        # Get some basic stats
        total_users = self.env['res.users'].search_count([('company_id', '=', company.id)])
        total_customers = self.env['res.partner'].search_count([
            ('company_id', '=', company.id),
            ('customer_rank', '>', 0)
        ])
        
        return f"""
Basic Company Information:
- Name: {company.name}
- Website: {company.website or 'Not set'}
- Email: {company.email or 'Not set'}
- Phone: {company.phone or 'Not set'}
- Address: {company.street or ''} {company.city or ''}, {company.country_id.name or ''}
- Total users: {total_users}
- Total customers: {total_customers}
"""
    
    def _get_sales_data(self, message):
        """Get sales data based on the user's message"""
        # Determine time period from message
        time_period = self._extract_time_period(message)
        
        # Get date range for the period
        date_from, date_to = self._get_date_range(time_period)
        
        # Query sales orders for the period
        domain = [
            ('state', 'in', ['sale', 'done']),
            ('date_order', '>=', date_from),
            ('date_order', '<=', date_to),
            ('company_id', '=', self.company_id.id)
        ]
        
        orders = self.env['sale.order'].search(domain)
        
        if not orders:
            return f"No sales data found for the period {date_from.strftime('%Y-%m-%d')} to {date_to.strftime('%Y-%m-%d')}."
        
        # Basic metrics
        total_orders = len(orders)
        total_amount = sum(orders.mapped('amount_total'))
        avg_order_value = total_amount / total_orders if total_orders else 0
        
        # Group by customer
        customer_data = {}
        for order in orders:
            customer_name = order.partner_id.name
            if customer_name in customer_data:
                customer_data[customer_name]['order_count'] += 1
                customer_data[customer_name]['total_amount'] += order.amount_total
            else:
                customer_data[customer_name] = {
                    'order_count': 1,
                    'total_amount': order.amount_total
                }
        
        # Get top customers
        top_customers = sorted(
            customer_data.items(), 
            key=lambda x: x[1]['total_amount'], 
            reverse=True
        )[:5]
        
        # Get product sales
        product_data = {}
        for order in orders:
            for line in order.order_line:
                product_name = line.product_id.name
                if product_name in product_data:
                    product_data[product_name]['qty'] += line.product_uom_qty
                    product_data[product_name]['amount'] += line.price_subtotal
                else:
                    product_data[product_name] = {
                        'qty': line.product_uom_qty,
                        'amount': line.price_subtotal
                    }
        
        # Get top products
        top_products = sorted(
            product_data.items(),
            key=lambda x: x[1]['amount'],
            reverse=True
        )[:5]
        
        # Format data
        result = f"""
Sales Data ({time_period}):
- Date Range: {date_from.strftime('%Y-%m-%d')} to {date_to.strftime('%Y-%m-%d')}
- Total Orders: {total_orders}
- Total Revenue: {total_amount:.2f}
- Average Order Value: {avg_order_value:.2f}

Top Customers:
"""
        for i, (customer, data) in enumerate(top_customers, 1):
            result += f"{i}. {customer}: {data['order_count']} orders, {data['total_amount']:.2f}\n"
        
        result += "\nTop Products:"
        for i, (product, data) in enumerate(top_products, 1):
            result += f"\n{i}. {product}: {data['qty']} units, {data['amount']:.2f}"
        
        return result
    
    def _get_inventory_data(self, message):
        """Get inventory data based on the user's message"""
        # Get products with low stock
        low_stock_domain = [
            ('type', '=', 'product'),
            ('qty_available', '<', 10),
            ('company_id', '=', self.company_id.id)
        ]
        
        low_stock_products = self.env['product.product'].search(low_stock_domain, limit=10)
        
        # Get products with highest stock value
        all_products = self.env['product.product'].search([
            ('type', '=', 'product'),
            ('company_id', '=', self.company_id.id)
        ])
        
        stock_values = [(p, p.qty_available * p.standard_price) for p in all_products]
        top_value_products = sorted(stock_values, key=lambda x: x[1], reverse=True)[:5]
        
        # Get recent stock moves
        today = fields.Date.today()
        one_month_ago = today - timedelta(days=30)
        
        recent_moves = self.env['stock.move'].search([
            ('company_id', '=', self.company_id.id),
            ('date', '>=', one_month_ago),
            ('state', '=', 'done')
        ], order='date desc', limit=5)
        
        # Format data
        result = f"""
Inventory Data (as of {today}):

Products with Low Stock (less than 10 units):
"""
        if low_stock_products:
            for i, product in enumerate(low_stock_products, 1):
                result += f"{i}. {product.name}: {product.qty_available} {product.uom_id.name}\n"
        else:
            result += "No products with low stock found.\n"
        
        result += "\nProducts with Highest Stock Value:"
        if top_value_products:
            for i, (product, value) in enumerate(top_value_products, 1):
                result += f"\n{i}. {product.name}: {product.qty_available} {product.uom_id.name}, Value: {value:.2f}"
        else:
            result += "\nNo products found."
        
        result += "\n\nRecent Stock Movements:"
        if recent_moves:
            for move in recent_moves:
                result += f"\n- {move.date.strftime('%Y-%m-%d')}: {move.product_id.name}, {move.product_uom_qty} {move.product_uom.name}, {move.location_id.name} → {move.location_dest_id.name}"
        else:
            result += "\nNo recent stock movements found."
        
        return result
    
    def _get_finance_data(self, message):
        """Get finance data based on the user's message"""
        # Determine time period from message
        time_period = self._extract_time_period(message)
        
        # Get date range for the period
        date_from, date_to = self._get_date_range(time_period)
        
        # Query invoices for the period
        invoice_domain = [
            ('move_type', '=', 'out_invoice'),
            ('invoice_date', '>=', date_from),
            ('invoice_date', '<=', date_to),
            ('state', '=', 'posted'),
            ('company_id', '=', self.company_id.id)
        ]
        
        invoices = self.env['account.move'].search(invoice_domain)
        
        # Query bills for the period
        bill_domain = [
            ('move_type', '=', 'in_invoice'),
            ('invoice_date', '>=', date_from),
            ('invoice_date', '<=', date_to),
            ('state', '=', 'posted'),
            ('company_id', '=', self.company_id.id)
        ]
        
        bills = self.env['account.move'].search(bill_domain)
        
        # Query payments for the period
        payment_domain = [
            ('date', '>=', date_from),
            ('date', '<=', date_to),
            ('state', '=', 'posted'),
            ('company_id', '=', self.company_id.id)
        ]
        
        payments = self.env['account.payment'].search(payment_domain)
        
        # Calculate metrics
        total_invoices = len(invoices)
        total_invoice_amount = sum(invoices.mapped('amount_total'))
        
        total_bills = len(bills)
        total_bill_amount = sum(bills.mapped('amount_total'))
        
        total_customer_payments = sum(p.amount for p in payments if p.partner_type == 'customer')
        total_vendor_payments = sum(p.amount for p in payments if p.partner_type == 'supplier')
        
        # Get overdue invoices
        today = fields.Date.today()
        overdue_domain = [
            ('move_type', '=', 'out_invoice'),
            ('invoice_date_due', '<', today),
            ('payment_state', 'in', ['not_paid', 'partial']),
            ('state', '=', 'posted'),
            ('company_id', '=', self.company_id.id)
        ]
        
        overdue_invoices = self.env['account.move'].search(overdue_domain, order='invoice_date_due')
        
        # Format data
        result = f"""
Finance Data ({time_period}):
- Date Range: {date_from.strftime('%Y-%m-%d')} to {date_to.strftime('%Y-%m-%d')}

Invoices:
- Total Invoices: {total_invoices}
- Total Value: {total_invoice_amount:.2f}
- Average Value: {(total_invoice_amount / total_invoices if total_invoices else 0):.2f}

Bills:
- Total Bills: {total_bills}
- Total Value: {total_bill_amount:.2f}
- Average Value: {(total_bill_amount / total_bills if total_bills else 0):.2f}

Payments:
- Customer Payments: {total_customer_payments:.2f}
- Vendor Payments: {total_vendor_payments:.2f}

Overdue Invoices:
"""
        if overdue_invoices:
            for i, inv in enumerate(overdue_invoices[:5], 1):
                days_overdue = (today - inv.invoice_date_due).days
                result += f"{i}. {inv.name}: {inv.partner_id.name}, {inv.amount_total:.2f}, {days_overdue} days overdue\n"
                
            if len(overdue_invoices) > 5:
                result += f"... and {len(overdue_invoices) - 5} more\n"
        else:
            result += "No overdue invoices found.\n"
        
        return result
    
    def _extract_time_period(self, message):
        """Extract time period from message"""
        message_lower = message.lower()
        
        # Define time periods and their keywords
        time_periods = {
            'today': ['today', 'hari ini'],
            'yesterday': ['yesterday', 'kemarin'],
            'this_week': ['this week', 'minggu ini', 'week'],
            'last_week': ['last week', 'minggu lalu'],
            'this_month': ['this month', 'bulan ini', 'month'],
            'last_month': ['last month', 'bulan lalu'],
            'this_quarter': ['this quarter', 'kuartal ini', 'quarter'],
            'last_quarter': ['last quarter', 'kuartal lalu'],
            'this_year': ['this year', 'tahun ini', 'year'],
            'last_year': ['last year', 'tahun lalu'],
        }
        
        # Check for each period
        for period, keywords in time_periods.items():
            if any(keyword in message_lower for keyword in keywords):
                return period
        
        # Default to this month
        return 'this_month'
    
    def _get_date_range(self, time_period):
        """Get date range for the given time period"""
        today = fields.Date.today()
        
        if time_period == 'today':
            return today, today
        elif time_period == 'yesterday':
            yesterday = today - timedelta(days=1)
            return yesterday, yesterday
        elif time_period == 'this_week':
            start_of_week = today - timedelta(days=today.weekday())
            return start_of_week, today
        elif time_period == 'last_week':
            end_of_last_week = today - timedelta(days=today.weekday() + 1)
            start_of_last_week = end_of_last_week - timedelta(days=6)
            return start_of_last_week, end_of_last_week
        elif time_period == 'this_month':
            start_of_month = today.replace(day=1)
            return start_of_month, today
        elif time_period == 'last_month':
            last_month_end = today.replace(day=1) - timedelta(days=1)
            last_month_start = last_month_end.replace(day=1)
            return last_month_start, last_month_end
        elif time_period == 'this_quarter':
            current_quarter = ((today.month - 1) // 3) + 1
            start_month = 3 * (current_quarter - 1) + 1
            start_of_quarter = today.replace(month=start_month, day=1)
            return start_of_quarter, today
        elif time_period == 'last_quarter':
            current_quarter = ((today.month - 1) // 3) + 1
            last_quarter = current_quarter - 1 if current_quarter > 1 else 4
            last_quarter_year = today.year if current_quarter > 1 else today.year - 1
            start_month = 3 * (last_quarter - 1) + 1
            end_month = start_month + 2
            start_date = datetime(last_quarter_year, start_month, 1).date()
            end_date = datetime(last_quarter_year, end_month, 1).date() + timedelta(days=31)
            end_date = end_date.replace(day=1) - timedelta(days=1)
            return start_date, end_date
        elif time_period == 'this_year':
            start_of_year = today.replace(month=1, day=1)
            return start_of_year, today
        elif time_period == 'last_year':
            last_year = today.year - 1
            start_date = datetime(last_year, 1, 1).date()
            end_date = datetime(last_year, 12, 31).date()
            return start_date, end_date
        
        # Default to this month
        start_of_month = today.replace(day=1)
        return start_of_month, today