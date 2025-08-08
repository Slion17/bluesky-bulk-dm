from http.server import BaseHTTPRequestHandler
import json
import traceback
import time

class handler(BaseHTTPRequestHandler):
    def do_POST(self):
        try:
            # Set CORS headers first
            self.send_response(200)
            self.send_header('Content-type', 'application/json')
            self.send_header('Access-Control-Allow-Origin', '*')
            self.send_header('Access-Control-Allow-Methods', 'POST, OPTIONS')
            self.send_header('Access-Control-Allow-Headers', 'Content-Type')
            self.end_headers()
            
            # Read the request body
            content_length = int(self.headers.get('Content-Length', 0))
            post_data = self.rfile.read(content_length)
            data = json.loads(post_data.decode('utf-8'))
            
            user_handle = data.get('userHandle')
            user_password = data.get('userPassword')
            target_handle = data.get('targetHandle')
            message = data.get('message')
            
            if not all([user_handle, user_password, target_handle, message]):
                self.wfile.write(json.dumps({
                    'success': False,
                    'error': 'Missing required fields (userHandle, userPassword, targetHandle, message)'
                }).encode())
                return
            
            # Validate message length
            if len(message) > 1000:
                self.wfile.write(json.dumps({
                    'success': False,
                    'error': 'Message too long. Maximum 1000 characters allowed.'
                }).encode())
                return
            
            # Import atproto inside the try block to catch import errors
            from atproto import Client, models
            from atproto.exceptions import RequestException
            
            # Login to Bluesky
            client = Client()
            try:
                client.login(user_handle, user_password)
            except Exception as e:
                self.wfile.write(json.dumps({
                    'success': False,
                    'error': f'Authentication failed: {str(e)}',
                    'error_type': 'AuthenticationError'
                }).encode())
                return
            
            # Get profile of target user to get their DID
            try:
                profile = client.app.bsky.actor.get_profile({'actor': target_handle})
                target_did = profile.did
            except Exception as e:
                self.wfile.write(json.dumps({
                    'success': False,
                    'error': f'Could not find user {target_handle}: {str(e)}',
                    'error_type': 'UserNotFound'
                }).encode())
                return
            
            # Try to send the DM
            try:
                # Note: The exact API endpoints for chat may vary depending on atproto version
                # We'll try multiple approaches to handle different API versions
                
                # Method 1: Try direct message sending (newer API)
                try:
                    # Some versions of atproto may have direct DM methods
                    if hasattr(client, 'send_message'):
                        result = client.send_message(target_did, message)
                    else:
                        # Method 2: Use chat conversation API
                        # First get or create conversation
                        convo_response = client.com.atproto.server.create_session if hasattr(client, 'com') else None
                        
                        # Try getting existing conversations
                        if hasattr(client, 'chat') and hasattr(client.chat, 'bsky'):
                            convos = client.chat.bsky.convo.list_convos({'limit': 50})
                            
                            # Look for existing conversation with target
                            existing_convo = None
                            if hasattr(convos, 'convos'):
                                for convo in convos.convos:
                                    if hasattr(convo, 'members'):
                                        member_dids = [getattr(m, 'did', '') for m in convo.members]
                                        if target_did in member_dids:
                                            existing_convo = convo.id
                                            break
                            
                            # Get or create conversation
                            if existing_convo:
                                convo_id = existing_convo
                            else:
                                # Create new conversation
                                new_convo = client.chat.bsky.convo.get_convo_for_members({
                                    'members': [target_did]
                                })
                                convo_id = new_convo.convo.id
                            
                            # Send message to conversation
                            send_result = client.chat.bsky.convo.send_message({
                                'convoId': convo_id,
                                'message': {'text': message}
                            })
                        else:
                            # Fallback: Try alternative chat API structure
                            # This handles different versions of the atproto library
                            raise Exception("Chat API not available in this atproto version")
                
                except Exception as chat_error:
                    # If chat API fails, try alternative approaches or provide helpful error
                    error_msg = str(chat_error).lower()
                    if 'not found' in error_msg or 'unknown' in error_msg:
                        raise Exception("Direct messaging may not be available in this version of Bluesky API")
                    else:
                        raise chat_error
                
                self.wfile.write(json.dumps({
                    'success': True,
                    'message': f'Successfully sent DM to {target_handle}',
                    'target_handle': target_handle
                }).encode())
                
            except Exception as e:
                error_message = str(e).lower()
                
                # Check for common DM-related errors
                if 'blocked' in error_message or 'block' in error_message:
                    self.wfile.write(json.dumps({
                        'success': False,
                        'error': f'Cannot send DM to {target_handle}: You may be blocked by this user',
                        'error_type': 'BlockedError'
                    }).encode())
                elif 'disabled' in error_message or 'not accepting' in error_message:
                    self.wfile.write(json.dumps({
                        'success': False,
                        'error': f'Cannot send DM to {target_handle}: User has DMs disabled',
                        'error_type': 'DMsDisabled'
                    }).encode())
                elif 'conversation' in error_message or 'convo' in error_message:
                    self.wfile.write(json.dumps({
                        'success': False,
                        'error': f'Cannot send DM to {target_handle}: Conversation error. User may not accept DMs from you.',
                        'error_type': 'ConversationError'
                    }).encode())
                else:
                    # Check for rate limiting
                    is_rate_limit = any([
                        "429" in str(e),
                        "rate" in error_message and "limit" in error_message,
                        "too many requests" in error_message,
                        "ratelimit" in error_message
                    ])
                    
                    if is_rate_limit:
                        self.wfile.write(json.dumps({
                            'success': False,
                            'error': 'Rate limit exceeded. Please slow down your requests.',
                            'error_type': 'RateLimit',
                            'retry_after': 60,
                            'target_handle': target_handle
                        }).encode())
                    else:
                        self.wfile.write(json.dumps({
                            'success': False,
                            'error': f'Failed to send DM to {target_handle}: {str(e)}',
                            'error_type': 'DMError',
                            'target_handle': target_handle
                        }).encode())
            
        except ImportError as e:
            # Handle missing dependencies
            self.wfile.write(json.dumps({
                'success': False,
                'error': f'Missing dependency: {str(e)}. Please ensure atproto library is installed.'
            }).encode())
            
        except json.JSONDecodeError as e:
            # Handle JSON parsing errors
            self.wfile.write(json.dumps({
                'success': False,
                'error': f'Invalid JSON data: {str(e)}'
            }).encode())
            
        except Exception as e:
            # Handle all other errors with detailed traceback
            error_details = {
                'success': False,
                'error': str(e),
                'error_type': type(e).__name__,
                'traceback': traceback.format_exc()
            }
            
            self.wfile.write(json.dumps(error_details).encode())
    
    def do_OPTIONS(self):
        # Handle preflight requests
        self.send_response(200)
        self.send_header('Access-Control-Allow-Origin', '*')
        self.send_header('Access-Control-Allow-Methods', 'POST, OPTIONS')
        self.send_header('Access-Control-Allow-Headers', 'Content-Type')
        self.end_headers()
        self.wfile.write(b'')
