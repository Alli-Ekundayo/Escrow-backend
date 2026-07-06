# Escrow-Backend

A comprehensive escrow system built with Django for the TrustFlow platform.

## 🛠️ Tech Stack
- **Backend**: Django, Django Rest Framework
- **Payments**: Nomba (Wallet & Payouts)
- **AI Integration**: DashScope (for legal drafting)
- **Authentication**: JWT (djangorestframework-simplejwt)
- **Testing**: Pytest, Mocking

## 🚀 Quick Start

### Prerequisites
Ensure you have the following installed:
- Python 3.10+
- PostgreSQL (optional, can use SQLite for dev)
- Nomba API Keys (for wallet creation)

### Setup

1. **Clone the repository**
   ```bash
   git clone <repository-url>
   cd Escrow-backend
   ```

2. **Create and activate a virtual environment**
   ```bash
   python3 -m venv venv
   source venv/bin/activate  # On Windows: venv\Scripts\activate
   ```

3. **Install dependencies**
   ```bash
   pip install -r requirements.txt
   ```

4. **Environment Variables**
   Create a `.env` file in the root directory (copy from `.env.example` if available):

   ```env
   SECRET_KEY=your_secret_key
   DEBUG=True
   ALLOWED_HOSTS=localhost,[IP_ADDRESS]

   # Database (SQLite for local, uncomment Postgres if needed)
   DATABASES={"default": {"ENGINE": "django.db.backends.sqlite3", "NAME": "db.sqlite3"}}
   # DATABASES={"default": {"ENGINE": "django.db.backends.postgresql", ...}}

   # Nomba Configuration
   NOMBA_PUBLIC_KEY=your_nomba_public_key
   NOMBA_SECRET_KEY=your_nomba_secret_key
   NOMBA_WEBHOOK_URL=http://localhost:8000/api/v1/payments/nomba/webhook/

   # DashScope (AI) Configuration
   DASHSCOPE_API_KEY=your_dashscope_key
   ```

5. **Run Migrations**
   ```bash
   python manage.py migrate
   ```

6. **Create Superuser (Optional)**
   ```bash
   python manage.py createsuperuser
   ```

7. **Run the Server**
   ```bash
   python manage.py runserver
   ```

## 📋 API Documentation

### Authentication

**Obtain JWT Token:**
```http
POST /api/v1/auth/login/
Content-Type: application/json

{
    "email": "[EMAIL_ADDRESS]",
    "password": "testpassword123"
}
```

**Response:**
```json
{
    "refresh": "eyJ0eXAiOiJKV1QiLCJhbGciOiJIUzI1NiJ9...",
    "access": "eyJ0eXAiOiJKV1QiLCJhbGciOiJIUzI1NiJ9..."
}
```

**Headers:** Use the `access` token in the `Authorization` header for protected endpoints:
```http
Authorization: Bearer <access_token>
```

### User Endpoints

#### Create User
```http
POST /api/v1/users/
Content-Type: application/json

{
    "email": "[EMAIL_ADDRESS]",
    "password": "password123",
    "first_name": "John",
    "last_name": "Doe",
    "phone_number": "+2348012345678",
    "role": "buyer"
}
```

#### Create Nomba Wallet (Pre-registration)
```http
POST /api/v1/payments/create-nomba-wallet/
Content-Type: application/json

{
    "email": "[EMAIL_ADDRESS]",
    "first_name": "John",
    "last_name": "Doe",
    "phone_number": "+2348012345678",
    "bank_code": "050",
    "account_number": "1234567890"
}
```

#### Verify Nomba Wallet
```http
POST /api/v1/payments/verify-nomba-wallet/
Content-Type: application/json

{
    "bank_code": "050",
    "account_number": "1234567890"
}
```

### Escrow Endpoints

#### Create Escrow Agreement
```http
POST /api/v1/escrow/agreements/
Content-Type: application/json

{
    "buyer": "[EMAIL_ADDRESS]",
    "seller": "[EMAIL_ADDRESS]",
    "amount": 50000.00,
    "currency": "NGN",
    "raw_conditions": "Deliver logo design within 3 days.",
    "deadline": "2024-12-31T10:00:00Z"
}
```

#### Get AI-Drafted Conditions
```http
POST /api/v1/escrow/agreements/draft-conditions/
Content-Type: application/json

{
    "raw_conditions": "Deliver logo design in 3 days.",
    "service_description": "Logo design services",
    "budget": 50000.00
}
```

#### Get Single Escrow Agreement
```http
GET /api/v1/escrow/agreements/{agreement_id}/
```

### Payment Endpoints

#### Fund Escrow (Buyer -> Wallet)
```http
POST /api/v1/payments/fund-escrow/{agreement_id}/
Content-Type: application/json

{
    "amount": 50000.00
}
```

#### Create Payment Link (for Escrow)
```http
POST /api/v1/payments/payment-links/{agreement_id}/
Content-Type: application/json

{
    "amount": 50000.00,
    "callback_url": "http://your-frontend/escrow/callback?id={order_id}"
}
```

#### Verify Payment Status (Optional callback)
```http
GET /api/v1/payments/verify-payment/{order_id}/
```

### Notification Endpoints

#### Nomba Webhook (For Escrow)
```http
POST /api/v1/payments/nomba/webhook/
Content-Type: application/json

{
    "event": "ORDER_COMPLETED",
    "data": { ... }
}
```
*Note: Requires specific signature verification configured.*

## 🧪 Testing

The project includes a comprehensive test suite for the payment and escrow logic.

**Run All Tests:**
```bash
python manage.py test payments.tests
python manage.py test escrow.tests
python manage.py test users.tests
```

**Key Test Scenarios Covered:**
1. **Token Caching**: Verifies Nomba token refresh and caching.
2. **Error Handling**: Simulates Nomba 502, 400, and timeout errors.
3. **Wallet Management**: Tests pre-registration and verification flows.
4. **Escrow Logic**: Covers agreement creation and status transitions.
5. **AI Integration**: Tests DashScope integration for condition drafting.

### Running Tests Without Network Calls
All tests use `unittest.mock.patch` to intercept network calls to Nomba and DashScope, ensuring fast and reliable execution without hitting external APIs.

## 📂 Project Structure

```
Escrow-backend/
├── payments/              # Payment logic, Nomba integration, escrow services
├── escrow/                # Escrow models, agreements, disputes, milestones
├── users/                 # User management and authentication
├── core/                  # Core utilities, config, middleware
└── api/                   # API endpoints and serializers
```