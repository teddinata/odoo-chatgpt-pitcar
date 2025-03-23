from odoo import http
from odoo.http import request
import json
import logging
import uuid
from datetime import datetime, timedelta, date

_logger = logging.getLogger(__name__)

class AIController(http.Controller):
    
    @http.route('/web/ai/chat', type='json', auth='user')
    def ai_chat_operations(self, **kwargs):
        """Main endpoint for all AI chat operations"""
        cr = request.env.cr
        
        # Create main savepoint for entire operation
        main_savepoint = f'operation_{uuid.uuid4().hex}'
        cr.execute(f'SAVEPOINT {main_savepoint}')
        
        try:
            # Coba dapatkan operasi dari kwargs dulu (parameter yang dikirim langsung ke method)
            operation = kwargs.get('operation')
            params = kwargs.get('params', {})
            
            # Jika tidak ada, coba dari request.jsonrequest jika tersedia
            if not operation and hasattr(request, 'jsonrequest'):
                operation = request.jsonrequest.get('operation')
                params = request.jsonrequest.get('params', {})
            
            # Jika masih tidak ada, coba dari request.params untuk HTTP requests
            if not operation:
                operation = request.params.get('operation')
                params = request.params.get('params', {})
                
                # Jika params masih string JSON, parse
                if isinstance(params, str):
                    try:
                        params = json.loads(params)
                    except:
                        params = {}
            
            if not operation:
                cr.execute(f'ROLLBACK TO SAVEPOINT {main_savepoint}')
                return {'success': False, 'error': 'Operation not specified'}
            
            # Map operations to methods
            operations_map = {
                'get_chat_list': self._get_chat_list,
                'create_chat': self._create_chat,
                'archive_chat': self._archive_chat,
                'restore_chat': self._restore_chat,
                'clear_chat': self._clear_chat,
                'get_chat_messages': self._get_chat_messages,
                'send_message': self._send_message,
                'get_settings': self._get_ai_settings,
                'update_settings': self._update_ai_settings,
                'export_chat': self._export_chat,
            }
            
            if operation not in operations_map:
                cr.execute(f'ROLLBACK TO SAVEPOINT {main_savepoint}')
                return {'success': False, 'error': f'Unknown operation: {operation}'}
            
            # Call the appropriate method
            result = operations_map[operation](params)
            
            # If operation was successful, commit the transaction
            if result.get('success', False):
                cr.execute(f'RELEASE SAVEPOINT {main_savepoint}')
            else:
                # Rollback to savepoint if operation failed
                cr.execute(f'ROLLBACK TO SAVEPOINT {main_savepoint}')
            
            return result
            
        except Exception as e:
            # Rollback the transaction if an error occurs
            cr.execute(f'ROLLBACK TO SAVEPOINT {main_savepoint}')
            _logger.error(f"Error in AI chat operation: {str(e)}", exc_info=True)
            return {'success': False, 'error': str(e)}
    
    def _get_chat_list(self, params):
        """Get chats for the current user, with option to include archived"""
        try:
            # Set default parameters
            include_archived = params.get('include_archived', False) if params else False
            
            # Build domain
            domain = [('user_id', '=', request.env.user.id)]
            
            # Filter active status only if not including archived
            if not include_archived:
                domain.append(('active', '=', True))
            
            # Get chats based on domain
            chats = request.env['ai.chat'].sudo().search(domain, order='last_message_date desc')
            
            result = []
            for chat in chats:
                # Get the last message
                last_message = chat.message_ids.sorted('create_date', reverse=True)[:1]
                
                # Include state and active status in response
                result.append({
                    'id': chat.id,
                    'name': chat.name,
                    'created_at': chat.create_date.isoformat(),
                    'last_message_date': chat.last_message_date.isoformat() if chat.last_message_date else None,
                    'last_message': last_message.content[:100] + '...' if last_message and last_message.content else None,
                    'total_messages': len(chat.message_ids),
                    'total_tokens': chat.total_tokens,
                    'topic': chat.topic or 'New Chat',
                    'summary': chat.summary or None,
                    'state': chat.state,  # Add state field
                    'active': chat.active,  # Add active field
                    'category': 'business' if 'business' in chat.name.lower() else 'general'  # Add category
                })
            
            return {
                'success': True,
                'chats': result
            }
            
        except Exception as e:
            _logger.error(f"Error getting chat list: {str(e)}")
            return {'success': False, 'error': str(e)}
    
    def _create_chat(self, params):
        """Create a new chat session"""
        try:
            # Dapatkan nama default jika tidak ada di params
            name = params.get('name')
            
            # Jika tidak ada nama yang diberikan, buat nama default
            if not name:
                # Metode 1: Gunakan tanggal dan waktu saat ini
                from datetime import datetime
                name = f"Chat {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}"
                
                # Metode 2: Jika fungsi _get_default_name memang seharusnya ada
                # try:
                #     name = f"Chat {request.env['ai.chat']._get_default_name()}"
                # except AttributeError:
                #     name = f"Chat {datetime.now().strftime('%Y-%m-%d')}"
            
            category = params.get('category', 'general')
            
            # Create a new chat session
            chat = request.env['ai.chat'].sudo().create({
                'name': name,
                'user_id': request.env.user.id,
                'company_id': request.env.company.id,
                'category': category,
            })
            
            return {
                'success': True,
                'chat_id': chat.id,
                'name': chat.name,
                'session_token': chat.session_token if hasattr(chat, 'session_token') else None
            }
        except Exception as e:
            _logger.error(f"Error creating chat session: {str(e)}")
            return {'success': False, 'error': str(e)}
    
    def _archive_chat(self, params):
        """Archive a chat"""
        try:
            chat_id = params.get('chat_id')
            if not chat_id:
                return {'success': False, 'error': 'Chat ID required'}
                
            # Get the chat
            chat = request.env['ai.chat'].sudo().browse(chat_id)
            
            # Check if chat exists and belongs to the user
            if not chat.exists() or chat.user_id.id != request.env.user.id:
                return {'success': False, 'error': 'Chat not found or access denied'}
            
            # Archive the chat
            chat.active = False
            
            return {'success': True, 'message': 'Chat archived successfully'}
            
        except Exception as e:
            _logger.error(f"Error archiving chat: {str(e)}")
            return {'success': False, 'error': str(e)}
    
    def _restore_chat(self, params):
        """Restore an archived chat"""
        try:
            chat_id = params.get('chat_id')
            if not chat_id:
                return {'success': False, 'error': 'Chat ID required'}
                
            # Get the chat
            chat = request.env['ai.chat'].sudo().browse(chat_id)
            
            # Check if chat exists and belongs to the user
            if not chat.exists() or chat.user_id.id != request.env.user.id:
                return {'success': False, 'error': 'Chat not found or access denied'}
            
            # Restore the chat
            chat.active = True
            
            return {'success': True, 'message': 'Chat restored successfully'}
            
        except Exception as e:
            _logger.error(f"Error restoring chat: {str(e)}")
            return {'success': False, 'error': str(e)}
    
    def _clear_chat(self, params):
        """Clear all messages in a chat"""
        try:
            chat_id = params.get('chat_id')
            if not chat_id:
                return {'success': False, 'error': 'Chat ID required'}
                
            # Get the chat
            chat = request.env['ai.chat'].sudo().browse(chat_id)
            
            # Check if chat exists and belongs to the user
            if not chat.exists() or chat.user_id.id != request.env.user.id:
                return {'success': False, 'error': 'Chat not found or access denied'}
            
            # Clear the messages
            chat.message_ids.unlink()
            
            return {'success': True, 'message': 'Chat cleared successfully'}
            
        except Exception as e:
            _logger.error(f"Error clearing chat: {str(e)}")
            return {'success': False, 'error': str(e)}
    
    def _get_chat_messages(self, params):
        """Get all messages for a chat"""
        try:
            chat_id = params.get('chat_id')
            if not chat_id:
                return {'success': False, 'error': 'Chat ID required'}
                
            # Get the chat
            chat = request.env['ai.chat'].sudo().browse(chat_id)
            
            # Check if chat exists and belongs to the user
            if not chat.exists() or chat.user_id.id != request.env.user.id:
                return {'success': False, 'error': 'Chat not found or access denied'}
            
            # Get messages
            messages = chat.message_ids.sorted('create_date')
            
            result = []
            for message in messages:
                result.append({
                    'id': message.id,
                    'message_id': message.message_uuid if hasattr(message, 'message_uuid') else f"msg_{message.id}",
                    'content': message.content,
                    'type': message.message_type,
                    'model_used': message.model_used if hasattr(message, 'model_used') else None,
                    'timestamp': message.create_date.isoformat(),
                    'token_count': message.token_count if hasattr(message, 'token_count') else 0,
                })
            
            return {
                'success': True,
                'chat': {
                    'id': chat.id,
                    'name': chat.name,
                    'user_id': chat.user_id.id,
                    'created_at': chat.create_date.isoformat(),
                    'last_message': chat.last_message_date.isoformat() if chat.last_message_date else None,
                },
                'messages': result
            }
            
        except Exception as e:
            _logger.error(f"Error getting chat messages: {str(e)}")
            return {'success': False, 'error': str(e)}
    
    def _send_message(self, params):
        """Mengirim pesan ke obrolan dan mendapatkan respons AI"""
        cr = request.env.cr
        
        # Create main savepoint for entire operation
        main_savepoint = 'send_message_main'
        cr.execute(f'SAVEPOINT {main_savepoint}')
        
        try:
            chat_id = params.get('chat_id')
            if not chat_id:
                cr.execute(f'ROLLBACK TO SAVEPOINT {main_savepoint}')
                return {'success': False, 'error': 'ID obrolan diperlukan'}

            # Ambil obrolan
            chat = request.env['ai.chat'].sudo().browse(chat_id)

            # Periksa apakah obrolan ada dan milik pengguna
            if not chat.exists() or chat.user_id.id != request.env.user.id:
                cr.execute(f'ROLLBACK TO SAVEPOINT {main_savepoint}')
                return {'success': False, 'error': 'Obrolan tidak ditemukan atau akses ditolak'}

            # Ambil konten pesan, model, dan mode kueri
            message_content = params.get('message', '')
            model = params.get('model', None)  # Model opsional
            query_mode = params.get('query_mode', 'auto')  # 'auto', 'business', 'general'

            if not message_content:
                cr.execute(f'ROLLBACK TO SAVEPOINT {main_savepoint}')
                return {'success': False, 'error': 'Konten pesan diperlukan'}

            # Send message with context to prevent auto-commit
            try:
                # Set flag to avoid nested transaction conflicts
                response = chat.with_context(no_commit=True, no_new_cr=True).send_message(
                    message_content, model, query_mode=query_mode
                )
            except Exception as e:
                _logger.error(f"Error in send_message: {str(e)}", exc_info=True)
                cr.execute(f'ROLLBACK TO SAVEPOINT {main_savepoint}')
                return {'success': False, 'error': str(e)}

            # Mendeteksi format respons yang berbeda
            if isinstance(response, dict) and 'error' in response:
                cr.execute(f'ROLLBACK TO SAVEPOINT {main_savepoint}')
                return {'success': False, 'error': response['error']}

            # Cek apakah obrolan telah diperbarui (misalnya, nama baru)
            chat_updated = False
            chat_data = None

            # Jika ada pembaruan pada obrolan setelah pesan dikirim
            if chat.write_date and chat.last_message_date and chat.write_date > chat.last_message_date:
                chat_updated = True
                chat_data = {
                    'id': chat.id,
                    'name': chat.name,
                    'topic': chat.topic,
                }

            # Commit the main savepoint as everything was successful
            cr.execute(f'RELEASE SAVEPOINT {main_savepoint}')

            # Pastikan respons memiliki struktur yang benar
            if isinstance(response, dict) and 'response' in response:
                response['chat_updated'] = chat_updated
                if chat_updated:
                    response['chat'] = chat_data
                return response

            # Jika respons tidak memiliki 'response' tetapi memiliki 'content'
            if isinstance(response, dict) and 'content' in response:
                return {
                    'success': True,
                    'response': {
                        'content': response['content'],
                        'model_used': response.get('model_used', model),
                        'token_count': response.get('token_count', 0),
                        'id': response.get('id', 0),
                        'message_id': response.get('message_uuid', '')
                    }
                }

            # Fallback untuk respons yang tidak sesuai format
            return {'success': True, 'response': response if isinstance(response, dict) else {'content': str(response)}}

        except Exception as e:
            cr.execute(f'ROLLBACK TO SAVEPOINT {main_savepoint}')
            _logger.error(f"Error saat mengirim pesan: {str(e)}", exc_info=True)
            return {'success': False, 'error': str(e)}
    
    def _get_ai_settings(self, params):
        """Get AI settings for the current user"""
        try:
            # Get user settings
            user_settings = request.env['ai.user.settings'].sudo().get_user_settings()
            
            # Get global settings
            api_key_set = bool(request.env['ir.config_parameter'].sudo().get_param('openai.api_key'))
            
            return {
                'success': True,
                'settings': {
                    'default_model': user_settings.default_model,
                    'daily_gpt4_limit': user_settings.daily_gpt4_limit,
                    'gpt4_usage_count': user_settings.gpt4_usage_count,
                    'remaining_gpt4': max(0, user_settings.daily_gpt4_limit - user_settings.gpt4_usage_count),
                    'fallback_to_gpt35': user_settings.fallback_to_gpt35,
                    'token_usage_this_month': user_settings.token_usage_this_month,
                    'api_key_configured': api_key_set,
                    'temperature': user_settings.temperature,
                    'max_tokens': user_settings.max_tokens,
                    'has_custom_prompt': bool(user_settings.custom_system_prompt),
                    'default_data_context': getattr(user_settings, 'default_data_context', 'balanced'),
                    'data_modules': getattr(user_settings, 'data_modules', []),
                }
            }
            
        except Exception as e:
            _logger.error(f"Error getting AI settings: {str(e)}")
            return {'success': False, 'error': str(e)}
    
    def _update_ai_settings(self, params):
        """Update AI settings for the current user"""
        try:
            # Get user settings
            user_settings = request.env['ai.user.settings'].sudo().get_user_settings()
            
            # Update settings
            values = {}
            
            if 'default_model' in params:
                values['default_model'] = params['default_model']
                
            if 'temperature' in params and isinstance(params['temperature'], (int, float)):
                values['temperature'] = min(max(params['temperature'], 0.0), 2.0)  # Limit to range 0-2
                
            if 'max_tokens' in params and isinstance(params['max_tokens'], int):
                values['max_tokens'] = min(max(params['max_tokens'], 100), 4000)  # Limit to range 100-4000
                
            if 'custom_system_prompt' in params:
                values['custom_system_prompt'] = params['custom_system_prompt']
                
            if 'fallback_to_gpt35' in params:
                values['fallback_to_gpt35'] = bool(params['fallback_to_gpt35'])
                
            if 'default_data_context' in params:
                values['default_data_context'] = params['default_data_context']
                
            if 'data_modules' in params and isinstance(params['data_modules'], list):
                values['data_modules'] = params['data_modules']
            
            # Apply updates if any
            if values:
                user_settings.write(values)
            
            # Return updated settings
            return self._get_ai_settings({})
            
        except Exception as e:
            _logger.error(f"Error updating AI settings: {str(e)}")
            return {'success': False, 'error': str(e)}
    
    def _export_chat(self, params):
        """Export chat to various formats"""
        try:
            chat_id = params.get('chat_id')
            if not chat_id:
                return {'success': False, 'error': 'Chat ID required'}
                
            export_format = params.get('format', 'json')
            if export_format not in ['json', 'txt', 'html', 'markdown', 'csv']:
                export_format = 'json'
            
            # Get the chat
            chat = request.env['ai.chat'].sudo().browse(chat_id)
            
            # Check if chat exists and belongs to the user
            if not chat.exists() or chat.user_id.id != request.env.user.id:
                return {'success': False, 'error': 'Chat not found or access denied'}
            
            # Get messages
            messages = chat.message_ids.sorted('create_date')
            
            # Export based on format
            exporter = getattr(self, f'_export_as_{export_format}', None)
            if not exporter:
                return {'success': False, 'error': f'Export format not supported: {export_format}'}
            
            data = exporter(chat, messages)
            
            return {
                'success': True,
                'data': data,
                'filename': f"chat_{chat.id}_{request.env['ai.chat']._get_export_timestamp()}.{self._get_file_extension(export_format)}"
            }
            
        except Exception as e:
            _logger.error(f"Error exporting chat: {str(e)}")
            return {'success': False, 'error': str(e)}
    
    def _get_business_data_context(self, data_context, data_modules, time_range, visualization):
        """Get business data context for the specified parameters"""
        try:
            # This is a placeholder for the actual implementation
            # In a real implementation, this would query Odoo models based on the
            # specified parameters and return structured data
            
            result = {
                'context_level': data_context,
                'modules': data_modules,
                'visualization': visualization
            }
            
            # Handle time range
            if time_range:
                range_type = time_range.get('range', 'this_month')
                result['time_range'] = range_type
                
                # Handle custom date range
                if range_type == 'custom':
                    result['start_date'] = time_range.get('start_date')
                    result['end_date'] = time_range.get('end_date')
            
            # Get actual business data - this is where you would implement
            # your specific business logic to query Odoo models
            result['data'] = self._get_business_data(result)
            
            return result
            
        except Exception as e:
            _logger.error(f"Error getting business data context: {str(e)}")
            return None
    
    def _get_business_data(self, context):
        """Get actual business data from Odoo models"""
        # This is a placeholder for the actual implementation
        # In a real implementation, this would query various Odoo models
        # based on the context and return the data
        
        # Example: If Sales module is requested, get sales data
        data = {}
        
        if 'Sales' in context.get('modules', []):
            # Query sales data
            data['sales'] = self._get_sales_data(context)
        
        if 'Inventory' in context.get('modules', []):
            # Query inventory data
            data['inventory'] = self._get_inventory_data(context)
        
        if 'Accounting' in context.get('modules', []):
            # Query accounting data
            data['accounting'] = self._get_accounting_data(context)
        
        return data
    
    def _get_sales_data(self, context):
        """Get sales data from Odoo models"""
        # Example implementation
        try:
            time_range = context.get('time_range', 'this_month')
            domain = [('state', 'in', ['sale', 'done'])]
            
            # Add date domain based on time range
            date_domain = request.env['ai.chat']._get_date_domain_for_range(
                time_range, 
                'date_order', 
                context.get('start_date'), 
                context.get('end_date')
            )
            if date_domain:
                domain.extend(date_domain)
            
            # Query sales orders
            sales_orders = request.env['sale.order'].sudo().search(domain)
            
            # Basic sales data
            result = {
                'total_orders': len(sales_orders),
                'total_revenue': sum(order.amount_total for order in sales_orders),
                'average_order_value': sum(order.amount_total for order in sales_orders) / len(sales_orders) if sales_orders else 0,
            }
            
            # Add detailed data based on context level
            if context.get('context_level') in ['balanced', 'comprehensive']:
                # Add top products
                result['top_products'] = self._get_top_products(sales_orders)
                
                # Add customer data
                result['top_customers'] = self._get_top_customers(sales_orders)
            
            if context.get('context_level') == 'comprehensive':
                # Add detailed time series data
                result['time_series'] = self._get_sales_time_series(sales_orders, context)
            
            return result
            
        except Exception as e:
            _logger.error(f"Error getting sales data: {str(e)}")
            return {}
    
    def _get_inventory_data(self, context):
        """Get inventory data from Odoo models"""
        # Example implementation
        return {
            'placeholder': 'Inventory data would be retrieved here'
        }
    
    def _get_accounting_data(self, context):
        """Get accounting data from Odoo models"""
        # Example implementation
        return {
            'placeholder': 'Accounting data would be retrieved here'
        }
    
    def _get_top_products(self, sales_orders):
        """Get top products from sales orders"""
        # Example implementation
        products = {}
        for order in sales_orders:
            for line in order.order_line:
                product_id = line.product_id.id
                if product_id not in products:
                    products[product_id] = {
                        'id': product_id,
                        'name': line.product_id.name,
                        'quantity': 0,
                        'revenue': 0,
                    }
                products[product_id]['quantity'] += line.product_uom_qty
                products[product_id]['revenue'] += line.price_subtotal
        
        # Sort by revenue
        top_products = sorted(
            products.values(), 
            key=lambda p: p['revenue'], 
            reverse=True
        )[:10]
        
        return top_products
    
    def _get_top_customers(self, sales_orders):
        """Get top customers from sales orders"""
        # Example implementation
        customers = {}
        for order in sales_orders:
            partner_id = order.partner_id.id
            if partner_id not in customers:
                customers[partner_id] = {
                    'id': partner_id,
                    'name': order.partner_id.name,
                    'orders': 0,
                    'revenue': 0,
                }
            customers[partner_id]['orders'] += 1
            customers[partner_id]['revenue'] += order.amount_total
        
        # Sort by revenue
        top_customers = sorted(
            customers.values(), 
            key=lambda c: c['revenue'], 
            reverse=True
        )[:10]
        
        return top_customers
    
    def _get_sales_time_series(self, sales_orders, context):
        """Get time series data for sales"""
        # Example implementation
        time_range = context.get('time_range', 'this_month')
        
        # Determine grouping period based on time range
        if time_range in ['today', 'yesterday']:
            grouping = 'hour'
        elif time_range in ['this_week', 'last_week']:
            grouping = 'day'
        elif time_range in ['this_month', 'last_month', 'last_3_months']:
            grouping = 'day'
        else:
            grouping = 'month'
        
        # Group by date
        date_groups = {}
        for order in sales_orders:
            # Format date based on grouping
            if grouping == 'hour':
                date_key = order.date_order.strftime('%Y-%m-%d %H:00:00')
            elif grouping == 'day':
                date_key = order.date_order.strftime('%Y-%m-%d')
            else:  # month
                date_key = order.date_order.strftime('%Y-%m-01')
            
            if date_key not in date_groups:
                date_groups[date_key] = {
                    'date': date_key,
                    'orders': 0,
                    'revenue': 0,
                }
            
            date_groups[date_key]['orders'] += 1
            date_groups[date_key]['revenue'] += order.amount_total
        
        # Convert to list and sort by date
        time_series = sorted(date_groups.values(), key=lambda d: d['date'])
        
        return time_series
    
    def _export_as_json(self, chat, messages):
        """Export chat as JSON"""
        result = {
            'chat': {
                'id': chat.id,
                'name': chat.name,
                'created_at': chat.create_date.isoformat(),
                'last_message': chat.last_message_date.isoformat() if chat.last_message_date else None,
            },
            'messages': []
        }
        
        for message in messages:
            result['messages'].append({
                'id': message.id,
                'message_id': message.message_uuid if hasattr(message, 'message_uuid') else f"msg_{message.id}",
                'content': message.content,
                'type': message.message_type,
                'model_used': message.model_used if hasattr(message, 'model_used') else None,
                'timestamp': message.create_date.isoformat(),
                'token_count': message.token_count if hasattr(message, 'token_count') else 0,
            })
        
        return result
    
    def _export_as_txt(self, chat, messages):
        """Export chat as plain text"""
        lines = []
        lines.append(f"Chat: {chat.name}")
        lines.append(f"Date: {chat.create_date.strftime('%Y-%m-%d %H:%M:%S')}")
        lines.append("-" * 80)
        
        for message in messages:
            sender = "User" if message.message_type == 'user' else "AI"
            timestamp = message.create_date.strftime('%Y-%m-%d %H:%M:%S')
            lines.append(f"{sender} ({timestamp}):")
            lines.append(message.content)
            lines.append("-" * 80)
        
        return "\n".join(lines)
    
    def _export_as_markdown(self, chat, messages):
        """Export chat as Markdown"""
        lines = []
        lines.append(f"# Chat: {chat.name}")
        lines.append(f"Date: {chat.create_date.strftime('%Y-%m-%d %H:%M:%S')}")
        lines.append("")
        
        for message in messages:
            sender = "User" if message.message_type == 'user' else "AI"
            timestamp = message.create_date.strftime('%Y-%m-%d %H:%M:%S')
            lines.append(f"## {sender} ({timestamp})")
            lines.append(message.content)
            lines.append("")
        
        return "\n".join(lines)
    
    def _export_as_html(self, chat, messages):
        """Export chat as HTML"""
        lines = []
        lines.append("<!DOCTYPE html>")
        lines.append("<html>")
        lines.append("<head>")
        lines.append(f"<title>Chat: {chat.name}</title>")
        lines.append("<style>")
        lines.append("body { font-family: Arial, sans-serif; max-width: 800px; margin: 0 auto; padding: 20px; }")
        lines.append(".message { margin-bottom: 20px; padding: 10px; border-radius: 10px; }")
        lines.append(".user { background-color: #f0f0f0; text-align: right; }")
        lines.append(".assistant { background-color: #e6f7ff; }")
        lines.append(".system { background-color: #fff3cd; }")
        lines.append(".header { color: #666; font-size: 0.8em; margin-bottom: 5px; }")
        lines.append(".content { white-space: pre-wrap; }")
        lines.append("</style>")
        lines.append("</head>")
        lines.append("<body>")
        lines.append(f"<h1>Chat: {chat.name}</h1>")
        lines.append(f"<p>Date: {chat.create_date.strftime('%Y-%m-%d %H:%M:%S')}</p>")
        
        for message in messages:
            msg_type = message.message_type
            sender = "User" if msg_type == 'user' else ("AI" if msg_type == 'assistant' else "System")
            timestamp = message.create_date.strftime('%Y-%m-%d %H:%M:%S')
            
            lines.append(f'<div class="message {msg_type}">')
            lines.append(f'<div class="header">{sender} ({timestamp})</div>')
            lines.append(f'<div class="content">{message.content}</div>')
            lines.append('</div>')
        
        lines.append("</body>")
        lines.append("</html>")
        
        return "\n".join(lines)
    
    def _export_as_csv(self, chat, messages):
        """Export chat as CSV"""
        lines = []
        lines.append("timestamp,sender,content")
        
        for message in messages:
            sender = "User" if message.message_type == 'user' else "AI"
            timestamp = message.create_date.strftime('%Y-%m-%d %H:%M:%S')
            # Escape content for CSV
            content = message.content.replace('"', '""')
            lines.append(f'"{timestamp}","{sender}","{content}"')
        
        return "\n".join(lines)
    
    def _get_file_extension(self, format):
        """Get file extension for export format"""
        extensions = {
            'json': 'json',
            'txt': 'txt',
            'html': 'html',
            'markdown': 'md',
            'csv': 'csv'
        }
        return extensions.get(format, 'txt')